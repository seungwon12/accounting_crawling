"""
전체 크롤링 워크플로우 오케스트레이터

기준서 단건(crawl_standard)과 전체(crawl_all) 크롤링을 조율합니다.
TOC 파싱 → 섹션별 문단 파싱 → 후처리 → JSON 저장 → 체크포인트 기록의
전체 파이프라인을 관리하며, 실패한 기준서의 자동 재시도를 수행합니다.

워크플로우 (crawl_standard):
    1. 기준서 메인 페이지 이동
    2. 기준서 제목 추출 (span "XXXX - 제목" 패턴)
    3. TOC 파싱 → 섹션 목록 추출
    4. 섹션별 문단 파싱 (재시도 포함)
    5. 문단 후처리
       - 중복 제거 (toc_path 깊이 우선)
       - 자기 기준서 내 문단 참조에 standard_number 채우기
       - 교차참조 범위(~) 전개
       - 잘못 매핑된 자기 문단 참조 보정
    6. 교차참조/QnA 인덱스 생성
    7. JSON 저장 + 체크포인트 기록
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from src.config import (
    BASE_URL, KIFRS_STANDARDS, KIFRIC_STANDARDS, CONCEPTUAL_FRAMEWORK, SPECIAL_DOCUMENTS,
    INTER_STANDARD_DELAY, MAX_RETRIES, RETRY_DELAYS,
)
from src.models import CrossReference, Paragraph, QnAReference, Standard, TocItem
from src.browser import BrowserManager, navigate_with_retry
from src.checkpoint import CheckpointManager, check_output_exists
from src.crawler.toc_parser import parse_toc, get_toc_sections, build_toc_path, flatten_toc
from src.crawler.section_parser import parse_section
from src.utils import logger, expand_paragraph_ranges


def _get_standard_type(standard_id: str) -> str:
    """
    기준서 번호에서 유형 문자열을 결정합니다.

    반환 규칙:
        - "CF" → "개념체계"
        - "MC", "MP" → "번역서"
        - "AO" → "적용의견서"
        - "1XXX" (첫 자리 1) → "기업회계기준서"
        - "2XXX" (첫 자리 2) → "기업회계기준해석서"
        - 그 외 → "기타"

    Args:
        standard_id: 기준서 번호 문자열 (예: "1001", "2101", "CF", "AO").

    Returns:
        유형 문자열 (예: "기업회계기준서").
    """
    # 특수문서 유형 매핑
    SPECIAL_TYPE_MAP = {
        "CF": "개념체계",
        "MC": "번역서",
        "MP": "번역서",
        "AO": "적용의견서",
    }
    if standard_id in SPECIAL_TYPE_MAP:
        return SPECIAL_TYPE_MAP[standard_id]
    prefix = standard_id[:1]
    if prefix == "1":
        return "기업회계기준서"
    if prefix == "2":
        return "기업회계기준해석서"
    return "기타"


def _get_standard_url(standard_id: str) -> str:
    """기준서 ID로 URL을 생성합니다.

    논리 ID와 사이트 내부 ID가 다른 경우 INTERNAL_ID_MAP으로 변환합니다.
    - CF → /s/1000 (개념체계)
    - MC → /s/1191 (경영진설명서)
    - MP → /s/1192 (중요성판단)
    - AO → /s/10121 (적용의견서)
    """
    # 논리 ID → 사이트 내부 ID 매핑
    INTERNAL_ID_MAP = {
        "CF": "1000",
        "MC": "1191",
        "MP": "1192",
        "AO": "10121",
    }
    internal_id = INTERNAL_ID_MAP.get(standard_id, standard_id)
    return f"{BASE_URL}/s/{internal_id}"


async def _extract_standard_title(page: Page, standard_id: str = "") -> str:
    """
    현재 페이지에서 기준서 제목을 추출합니다.
    사이트에서 제목은 "1001 - 재무제표 표시" 형태의 span에 있습니다.

    적용의견서(AO/10121) 등 특수문서는 번호-제목 패턴이 없으므로
    페이지 상단 h1/h2 또는 첫 번째 의미있는 제목 텍스트를 fallback으로 사용합니다.
    """
    title = await page.evaluate("""
        () => {
            // 패턴 1: "XXXX - 한글제목" 형태의 텍스트 노드 탐색
            // 기준서 번호(숫자 또는 CF) + " - " + 제목 패턴
            const allEls = Array.from(document.querySelectorAll('span, div, p, h1, h2, h3'));
            for (const el of allEls) {
                const text = el.textContent?.trim() || '';
                // "1001 - 재무제표 표시" 또는 "CF - 재무보고를 위한 개념체계" 형태
                const match = text.match(/^([0-9]{4,5}|CF)\\s*-\\s*(.+)$/);
                if (match && text.length < 100 && el.children.length === 0) {
                    return match[2].trim();
                }
            }
            // 패턴 2 (CF fallback): 번호 없이 "재무보고를 위한 개념체계" 형태의 span
            // CF 페이지는 내부 ID 1000으로 접근하므로 "1000 - ..." 패턴이 없을 수 있음
            const cfEl = allEls.find(el => {
                const text = el.textContent?.trim() || '';
                return text === '재무보고를 위한 개념체계' && el.children.length === 0;
            });
            if (cfEl) return cfEl.textContent.trim();
            // 패턴 3 (특수문서 fallback): h1 또는 h2의 첫 번째 의미있는 텍스트
            // 적용의견서(10121) 등 번호-제목 패턴이 없는 문서용
            const headings = Array.from(document.querySelectorAll('h1, h2'));
            for (const h of headings) {
                const text = h.textContent?.trim() || '';
                // "공유하기" 등 UI 버튼 텍스트 제외
                if (text && text.length > 2 && text.length < 100 && !text.includes('공유하기')) {
                    return text;
                }
            }
            return '';
        }
    """)
    if title:
        return title.strip()
    return "제목 미확인"


def _fill_self_standard_number(paragraphs: list[Paragraph], standard_id: str) -> None:
    """
    같은 기준서 내 문단 참조의 standard_number를 현재 기준서 번호로 채웁니다.

    JS 파싱 단계에서 mundan-finder 직전에 std-finder가 없으면
    associated_standard가 None으로 설정됩니다. 이는 같은 기준서 내 참조를
    의미하므로 current standard_id로 채워야 인덱스 구축 시 정확한 매핑이 됩니다.

    Args:
        paragraphs: 크롤링된 전체 문단 목록 (in-place 수정).
        standard_id: 현재 기준서 번호 (예: "1001").
    """
    for para in paragraphs:
        for ref in para.cross_references:
            if ref.type == "paragraph" and ref.standard_number is None:
                ref.standard_number = standard_id


def _expand_cross_reference_ranges(paragraphs: list[Paragraph]) -> None:
    """
    모든 교차참조의 paragraph_ids 범위(~)를 실제 문단 목록 기반으로 전개합니다.

    "15~35" 같은 범위 표기를 현재 기준서의 실제 문단 순서를 기준으로
    개별 문단 번호 목록으로 전개합니다. BC1, 40A 등 비숫자 번호도 처리합니다.
    utils.expand_paragraph_ranges에 전처리를 위임합니다.

    Args:
        paragraphs: 크롤링된 전체 문단 목록 (in-place 수정).
    """
    all_numbers = [p.number for p in paragraphs]
    for para in paragraphs:
        for ref in para.cross_references:
            if ref.type == "paragraph" and ref.paragraph_ids:
                ref.paragraph_ids = expand_paragraph_ranges(ref.paragraph_ids, all_numbers)


def _is_likely_self_reference(text: str, ref: "CrossReference", all_refs: list["CrossReference"]) -> bool:
    """
    단일 문단 참조가 자기 기준서 참조인지 텍스트 문맥으로 판별합니다.

    3가지 휴리스틱 적용:
    1. "이 기준서" 패턴: 참조 앞 40자에 "이 기준서" 포함 (예: 139A)
    2. "개정/삭제/추가" 패턴: 참조 뒤 15자에 해당 동사 포함 (예: 139N, 139Q)
    3. 문장 경계 패턴: 연관된 std-finder와 현재 mundan-finder 사이에 "다. " 존재 (예: 문단 76)

    Args:
        text: 현재 문단의 텍스트.
        ref: 보정 대상 교차참조 (type=="paragraph", len==1).
        all_refs: 해당 문단의 전체 교차참조 목록.

    Returns:
        True이면 자기 기준서 참조로 보정해야 함.
    """
    display = ref.display_text
    pos = text.find(display)
    if pos < 0:
        return False

    # 1) "이 기준서" 패턴: 참조 텍스트 앞 40자 이내에 "이 기준서" 존재
    before = text[max(0, pos - 40):pos]
    if "이 기준서" in before:
        return True

    # 2) "개정/삭제/추가" 패턴: 참조 텍스트 뒤 15자 이내에 해당 동사 존재
    after = text[pos + len(display):pos + len(display) + 15]
    if re.search(r"[를을이가]\s*(?:개정|삭제|추가)", after):
        return True

    # 3) 문장 경계 패턴: 연관된 std-finder와 현재 mundan 사이에 문장 종결 "다. " 존재
    assoc_std = ref.standard_number
    for std_ref in all_refs:
        if std_ref.type == "standard" and std_ref.standard_number == assoc_std:
            std_pos = text.rfind(std_ref.display_text, 0, pos)
            if std_pos >= 0:
                between = text[std_pos + len(std_ref.display_text):pos]
                if re.search(r"다\.\s", between):
                    return True
            break

    return False


def _correct_self_paragraph_references(paragraphs: list[Paragraph], standard_id: str) -> None:
    """
    다른 기준서로 잘못 매핑된 자기 문단 참조를 보정합니다.

    std-finder(다른 기준서) 바로 뒤에 mundan-finder가 오면 JS가 해당 다른 기준서의
    문단 참조로 매핑합니다. 두 가지 방법으로 보정:
    1. 복수 문단: 참조 문단 ID의 과반수가 현재 기준서에 존재하면 보정 (기존 로직)
    2. 단일 문단: 텍스트 문맥 휴리스틱으로 자기 참조 여부 판별 (_is_likely_self_reference)

    예시: 문단 139O에서 "1109호 ... 문단 7, 68" → 실제로는 1001호 자기 문단.

    Args:
        paragraphs: 크롤링된 전체 문단 목록 (in-place 수정).
        standard_id: 현재 기준서 번호.
    """
    all_numbers = {p.number for p in paragraphs}
    for para in paragraphs:
        for ref in para.cross_references:
            if (
                ref.type == "paragraph"
                and ref.standard_number
                and ref.standard_number != standard_id
                and ref.paragraph_ids
            ):
                if len(ref.paragraph_ids) >= 2:
                    # 복수 문단: 과반수가 현재 기준서에 존재하면 보정
                    matching = sum(1 for pid in ref.paragraph_ids if pid in all_numbers)
                    if matching / len(ref.paragraph_ids) >= 0.5:
                        ref.standard_number = standard_id
                else:
                    # 단일 문단: 텍스트 문맥 휴리스틱으로 자기 참조 여부 판별
                    # (단순히 현재 기준서에 같은 번호가 존재한다고 보정하면 오탐 위험)
                    if _is_likely_self_reference(para.text, ref, para.cross_references):
                        ref.standard_number = standard_id


def _build_cross_references_index(paragraphs: list[Paragraph]) -> dict[str, list[str]]:
    """
    기준서 번호 → 해당 기준서를 참조하는 문단 번호 목록 인덱스를 생성합니다.

    예시: {"1034": ["4", "한10.1"], "1110": ["4", "139H"]}
    "기준서 1034를 참조하는 문단이 어디인가?"를 O(1)로 조회할 수 있습니다.

    Args:
        paragraphs: 크롤링된 전체 문단 목록.

    Returns:
        {참조된_기준서_번호: [참조하는_문단_번호, ...]} 형태의 딕셔너리.
        중복 문단 번호는 제거됩니다.
    """
    index: dict[str, list[str]] = {}
    for para in paragraphs:
        for ref in para.cross_references:
            if ref.type == "standard" and ref.standard_number:
                index.setdefault(ref.standard_number, [])
                if para.number not in index[ref.standard_number]:
                    index[ref.standard_number].append(para.number)
    return index


def _build_qna_index(paragraphs: list[Paragraph]) -> dict[str, list[str]]:
    """
    질의회신 ID → 관련 문단 번호 목록 인덱스를 생성합니다.

    예시: {"2020-I-KQA009": ["4", "38"], "SSI-35569": ["7", "96"]}
    "이 QnA가 어느 문단과 관련 있는가?"를 O(1)로 조회할 수 있습니다.

    Args:
        paragraphs: 크롤링된 전체 문단 목록.

    Returns:
        {QnA_ID: [관련_문단_번호, ...]} 형태의 딕셔너리.
        중복 문단 번호는 제거됩니다.
    """
    index: dict[str, list[str]] = {}
    for para in paragraphs:
        for qna in para.qna_references:
            index.setdefault(qna.qna_id, [])
            if para.number not in index[qna.qna_id]:
                index[qna.qna_id].append(para.number)
    return index


def _save_standard_json(standard: Standard, output_dir: Path) -> None:
    """
    기준서 데이터를 JSON 파일로 저장합니다.

    Pydantic v2의 model_dump(mode='json')으로 직렬화하여
    output_dir/{standard_id}.json에 UTF-8 인코딩으로 기록합니다.

    Args:
        standard: 저장할 기준서 데이터 모델.
        output_dir: 출력 디렉토리 경로. 존재하지 않으면 자동 생성됩니다.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{standard.standard_id}.json"

    # Pydantic v2: model_dump(mode='json')으로 JSON 직렬화
    data = standard.model_dump(mode="json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(
        "저장됨: %s (문단 %d개, 섹션 %d개)",
        output_file,
        len(standard.paragraphs),
        len(set(p.section_id for p in standard.paragraphs)),
    )


async def crawl_standard(
    page: Page,
    standard_id: str,
    output_dir: Path,
    checkpoint: CheckpointManager,
) -> Optional[Standard]:
    """
    단일 기준서를 크롤링합니다.

    워크플로우:
    1. 기준서 페이지 이동
    2. TOC 파싱
    3. 섹션별 문단 파싱
    4. toc_path 계산
    5. 인덱스 생성
    6. JSON 저장
    """
    url = _get_standard_url(standard_id)
    standard_type = _get_standard_type(standard_id)

    logger.info("=" * 60)
    logger.info("크롤링 시작: %s %s", standard_type, standard_id)
    logger.info("URL: %s", url)

    checkpoint.set_current(standard_id)

    # 1. 기준서 메인 페이지 이동
    success = await navigate_with_retry(page, url)
    if not success:
        logger.error("기준서 페이지 이동 실패: %s", url)
        return None

    # 2. 기준서 제목 추출 (특수문서는 standard_id 전달로 fallback 활성화)
    title = await _extract_standard_title(page, standard_id)
    # 제목 추출 실패 시 SPECIAL_DOCUMENTS 설정값 사용
    if title == "제목 미확인":
        special_doc = next((d for d in SPECIAL_DOCUMENTS if d["id"] == standard_id), None)
        if special_doc:
            title = special_doc["title"]
    logger.info("제목: %s", title)

    # 3. TOC 파싱
    toc_tree, flat_items = await parse_toc(page)
    logger.info("TOC 항목 수: %d (트리 루트: %d개)", len(flat_items), len(toc_tree))

    # 4. 실제 섹션 목록 추출 (level 2 이상)
    # 비리프 섹션에도 고유 도입 문단(BC1, IG1~6, 81/81A/81B 등)이 있으므로 항상 부모 포함
    include_parents = True
    sections = get_toc_sections(flat_items, include_parents=include_parents)
    logger.info("파싱할 섹션 수: %d개", len(sections))

    if not sections:
        logger.warning("섹션을 찾을 수 없습니다: %s", standard_id)
        checkpoint.mark_standard_failed(standard_id, "섹션 없음 — URL 또는 파싱 오류")
        return None

    # 5. 섹션별 문단 파싱
    all_paragraphs: list[Paragraph] = []
    seen_urls: set[str] = set()  # 중복 URL 방지

    for i, section in enumerate(sections):
        section_url = section["href"]
        section_id = section["section_id"]
        section_title = section["title"]

        # 중복 URL 스킵
        if section_url in seen_urls:
            logger.debug("중복 섹션 스킵: %s", section_url)
            continue
        seen_urls.add(section_url)

        checkpoint.set_current(standard_id, section_id)

        # toc_path 계산
        toc_path = build_toc_path(section_id, toc_tree)

        logger.info(
            "[%d/%d] 섹션 파싱: %s (level=%d)",
            i + 1, len(sections), section_title, section["level"]
        )

        # 자유형식 여부 (li[data-paranum] 없는 페이지)
        is_free_content = section.get("is_free_content", False)

        # 섹션 문단 파싱 (재시도 포함)
        paragraphs = []
        for attempt in range(MAX_RETRIES):
            try:
                paragraphs = await parse_section(
                    page=page,
                    section_url=section_url,
                    section_id=section_id,
                    section_title=section_title,
                    toc_path=toc_path,
                    is_free_content=is_free_content,
                )
                break
            except Exception as e:
                logger.warning("섹션 파싱 실패 (시도 %d): %s - %s", attempt + 1, section_title, e)
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    await asyncio.sleep(delay)

        all_paragraphs.extend(paragraphs)
        logger.debug("문단 %d개 추가됨 (누적: %d개)", len(paragraphs), len(all_paragraphs))

    # 안전망: 문단 번호 기반 중복 제거 (toc_path가 더 깊은 버전 유지)
    seen: dict[str, Paragraph] = {}
    for para in all_paragraphs:
        if para.number not in seen:
            seen[para.number] = para
        else:
            existing_depth = seen[para.number].toc_path.count("|")
            new_depth = para.toc_path.count("|")
            if new_depth > existing_depth:
                seen[para.number] = para
    deduped = list(seen.values())
    if len(deduped) < len(all_paragraphs):
        logger.info("문단 중복 제거: %d개 → %d개", len(all_paragraphs), len(deduped))
    all_paragraphs = deduped

    logger.info("총 문단 수: %d개", len(all_paragraphs))

    # 5.4. 같은 기준서 내 문단 참조에 standard_number 채우기
    _fill_self_standard_number(all_paragraphs, standard_id)

    # 5.5. 교차참조 범위(~) 전개: 문서 순서 기반 인덱싱으로 비숫자 문단 번호도 처리
    _expand_cross_reference_ranges(all_paragraphs)

    # 5.6. 잘못 매핑된 자기 문단 참조 보정 (std-finder 컨텍스트 오염으로 인한 오매핑)
    _correct_self_paragraph_references(all_paragraphs, standard_id)

    # 6. 인덱스 생성
    cross_ref_index = _build_cross_references_index(all_paragraphs)
    qna_index = _build_qna_index(all_paragraphs)

    # 7. Standard 객체 생성
    standard = Standard(
        standard_id=standard_id,
        standard_type=standard_type,
        title=title,
        url=url,
        crawled_at=datetime.now(timezone.utc).isoformat(),
        toc=toc_tree,
        paragraphs=all_paragraphs,
        cross_references_index=cross_ref_index,
        qna_index=qna_index,
    )

    # 8. JSON 저장
    _save_standard_json(standard, output_dir)

    # 9. 체크포인트 완료 표시
    checkpoint.mark_standard_completed(standard_id)

    return standard


async def crawl_all(
    output_dir: Path,
    checkpoint: CheckpointManager,
    headless: bool = True,
    target_standard: Optional[str] = None,
    resume: bool = False,
) -> None:
    """
    전체 기준서/해석서/개념체계를 크롤링합니다.

    Args:
        output_dir: 출력 디렉토리
        checkpoint: 체크포인트 매니저
        headless: 헤드리스 모드 여부
        target_standard: 특정 기준서만 크롤링 (None이면 전체)
        resume: True이면 체크포인트에서 재개
    """
    from src.browser import BrowserManager

    # 크롤링 대상 목록 결정
    if target_standard:
        all_standards = [target_standard]
    else:
        # 기준서 + 해석서 + 개념체계 + 특수문서(경영진설명서, 중요성판단, 적용의견서)
        all_standards = (
            KIFRS_STANDARDS
            + KIFRIC_STANDARDS
            + CONCEPTUAL_FRAMEWORK
            + [d["id"] for d in SPECIAL_DOCUMENTS]
        )

    logger.info("크롤링 대상: %d개 기준서", len(all_standards))

    async with BrowserManager(headless=headless) as browser:
        page = await browser.new_page()

        for i, standard_id in enumerate(all_standards):
            # 재개 모드: 이미 출력 JSON이 있으면 스킵
            if resume and check_output_exists(standard_id, output_dir):
                logger.info("[%d/%d] 스킵 (이미 완료): %s", i + 1, len(all_standards), standard_id)
                continue

            # 체크포인트 확인
            if checkpoint.is_standard_completed(standard_id):
                logger.info("[%d/%d] 스킵 (체크포인트): %s", i + 1, len(all_standards), standard_id)
                continue

            try:
                result = await crawl_standard(page, standard_id, output_dir, checkpoint)
                if result is None:
                    # navigate_with_retry 실패 등 조용한 실패
                    checkpoint.mark_standard_failed(standard_id, "페이지 이동 실패")
            except Exception as e:
                logger.error("기준서 크롤링 중 예상치 못한 오류: %s - %s", standard_id, e)
                checkpoint.mark_standard_failed(standard_id, str(e))
                logger.info("오류 발생, 다음 기준서로 계속...")

            # 기준서 간 딜레이
            if i < len(all_standards) - 1:
                await asyncio.sleep(INTER_STANDARD_DELAY)

        # 실패한 기준서 재시도 (1회) — 현재 실행 대상인 기준서만 필터링
        all_set = set(all_standards)
        failed = {k: v for k, v in checkpoint.get_failed_standards().items() if k in all_set}
        if failed:
            logger.info("=" * 60)
            logger.info("실패한 기준서 %d개 재시도...", len(failed))
            failed_items = list(failed.items())
            for retry_idx, (standard_id, reason) in enumerate(failed_items):
                logger.info("[재시도 %d/%d] %s (이전 실패 원인: %s)", retry_idx + 1, len(failed_items), standard_id, reason)
                try:
                    result = await crawl_standard(page, standard_id, output_dir, checkpoint)
                    if result is not None:
                        checkpoint.clear_failed(standard_id)
                    else:
                        checkpoint.mark_standard_failed(standard_id, "재시도 실패: 페이지 이동 실패")
                except Exception as e:
                    checkpoint.mark_standard_failed(standard_id, f"재시도 실패: {e}")
                    logger.error("재시도 중 오류: %s - %s", standard_id, e)

                if retry_idx < len(failed_items) - 1:
                    await asyncio.sleep(INTER_STANDARD_DELAY)

    # 완료 요약
    completed = checkpoint.get_completed_standards()
    final_failed = checkpoint.get_failed_standards()
    logger.info("=" * 60)
    logger.info(
        "크롤링 완료! 성공: %d개, 실패: %d개, 전체: %d개",
        len(completed), len(final_failed), len(all_standards)
    )
    if final_failed:
        logger.warning("실패한 기준서 목록:")
        for sid, reason in final_failed.items():
            logger.warning("  - %s: %s", sid, reason)
