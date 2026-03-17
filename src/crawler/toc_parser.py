"""
TOC(목차) 파서 - level 속성 기반 스택 트리 구축

KASB 기준서 페이지의 aside 내 TOC를 파싱하여 계층 트리를 구축합니다.
사이트 TOC는 중첩 없는 완전 평탄한 <ol> 구조이며, 각 a[href] 요소의
level 속성(0~5, 소수점 포함)만으로 계층 관계를 결정합니다.

주요 함수:
    - parse_toc: 현재 페이지의 TOC를 파싱하여 (트리, 평탄목록) 반환
    - get_toc_sections: 실제 문단 내용이 있는 섹션만 필터링
    - build_toc_path: 섹션 ID → "본문|적용범위" 형태의 경로 문자열 변환
    - flatten_toc: 트리를 깊이 우선 순서로 평탄화

특이사항:
    - null level 항목(저작권 등): 항상 루트, 스택에 추가 안 함
    - "실무적용지침" 등: level=0이 level=2+ 자식을 가진 뒤 level=1이 오면
      해당 level=1을 자식이 아닌 루트(형제)로 처리
    - level 소수점('1.5'): float()로 변환하여 비교
"""
import hashlib
from typing import Optional

from playwright.async_api import Page

from src.models import TocItem
from src.utils import logger, extract_title_and_range


def _make_section_id(href: str, title: str) -> str:
    """
    섹션 고유 ID를 생성합니다.
    href에서 fragment(#) 부분을 추출하거나, 없으면 제목 기반 해시를 사용합니다.
    """
    if "#" in href:
        return href.split("#")[-1]
    # href에서 마지막 경로 세그먼트를 ID로 사용
    path_part = href.rstrip("/").split("/")[-1]
    if path_part and path_part not in ("standard", ""):
        return path_part
    # fallback: 제목 기반 해시
    return hashlib.md5(title.encode()).hexdigest()[:8]


def _build_toc_tree(raw_items: list[dict]) -> list[TocItem]:
    """
    평탄한 TOC 항목 목록에서 계층 트리를 구축합니다.

    스택 기반 알고리즘:
    - 스택 원소: [level, TocItem, has_direct_l2_children]
      - has_direct_l2_children: level=0 항목이 level=2+ 자식을 직접 가졌는지 여부

    특이사항 1: null level 항목(저작권 등)은 항상 루트, 스택에 추가하지 않음

    특이사항 2: "실무적용지침" 문제 해결
    - TOC는 HTML 중첩 없이 완전 평탄한 <ol> 구조이며, level 속성이 유일한 계층 정보
    - level=0 항목(본문)이 level=2+ 직접 자식을 보유한 상태에서 level=1이 등장하면,
      해당 level=1은 level=0의 자식이 아닌 ROOT로 처리
    - 예: 본문(0) → 목적(2) → ... → 실무적용지침(1) → 결론도출근거(0)
      실무적용지침은 본문의 자식이 아니라 본문의 형제(ROOT)
    - 반면: 결론도출근거(0) → IAS 1 결론도출근거(1)
      결론도출근거가 level=2 자식 없이 바로 level=1이 오면 정상 자식으로 처리
    """
    roots: list[TocItem] = []
    # 스택 원소: [level, TocItem, has_direct_l2_children]
    # 리스트를 사용하여 has_direct_l2_children 플래그를 가변적으로 수정 가능
    stack: list[list] = []

    for raw in raw_items:
        raw_level = raw.get("level")
        raw_title = raw.get("title", "").strip()
        href = raw.get("href", "")
        is_group = raw.get("is_group", False)

        if not raw_title:
            continue

        # 제목과 문단 범위 분리
        title, para_range = extract_title_and_range(raw_title)
        section_id = _make_section_id(href, title)

        item = TocItem(
            level=None if raw_level is None else float(raw_level),
            title=title,
            section_id=section_id,
            href=href,
            paragraph_range=para_range,
            children=[],
        )

        # 그룹 헤더 항목 (div[to] 요소: "적용사례 실무적용지침", "기타 참고사항" 등)
        # → 항상 루트로 처리, virtual level=0으로 스택에 추가
        # → 이후 level=1 항목들이 그룹 헤더의 자식이 됨
        if is_group:
            while stack and stack[-1][0] >= 0:
                stack.pop()
            roots.append(item)
            stack.append([0, item, False])
            continue

        # null level 항목 (저작권 등): 항상 루트, 스택에 추가하지 않음
        if raw_level is None:
            roots.append(item)
            continue

        level = float(raw_level)

        # 스택에서 현재 레벨 이상인 항목들을 팝
        while stack and stack[-1][0] >= level:
            stack.pop()

        # 특이사항 2: level=1~1.x 항목이 level=2+ 자식을 가진 level=0 바로 위에 있을 때
        # → 해당 level=1.x을 ROOT로 처리 (level=0의 형제)
        if 1 <= level < 2 and stack and stack[-1][0] == 0 and stack[-1][2]:
            stack.pop()

        if not stack:
            roots.append(item)
        else:
            stack[-1][1].children.append(item)
            # level=0 항목이 level=2+ 자식을 직접 받으면 플래그 설정
            if stack[-1][0] == 0 and level >= 2:
                stack[-1][2] = True

        stack.append([level, item, False])

    return roots


async def parse_toc(page: Page) -> tuple[list[TocItem], list[dict]]:
    """
    현재 페이지의 TOC를 파싱하여 트리 구조를 반환합니다.

    Returns:
        (toc_tree, flat_items): 트리 구조와 평탄한 항목 목록
        flat_items는 섹션 이동 순서를 위해 별도 반환
    """
    logger.debug("TOC 파싱 시작")

    # 브라우저 내에서 한 번에 모든 TOC 링크 추출
    # aside 내 a[href] 링크 + div[to] 그룹 헤더를 DOM 순서대로 수집
    raw_items: list[dict] = await page.evaluate("""
        () => {
            const aside = document.querySelector('aside')
                || document.querySelector('[role="complementary"]');

            if (aside) {
                // a[href] 링크와 div[to] 그룹 헤더를 모두 수집
                const allItems = [];

                aside.querySelectorAll('a[href]').forEach(a => {
                    allItems.push({ el: a, isGroup: false });
                });
                aside.querySelectorAll('div[to]').forEach(div => {
                    allItems.push({ el: div, isGroup: true });
                });

                // DOM 문서 순서로 정렬
                allItems.sort((a, b) => {
                    const pos = a.el.compareDocumentPosition(b.el);
                    return pos & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
                });

                const results = [];
                allItems.forEach(({ el, isGroup }) => {
                    if (isGroup) {
                        // div[to] 그룹 헤더: img 제거 후 텍스트 추출
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll('img').forEach(img => img.remove());
                        const title = clone.textContent?.trim() || '';
                        if (title) {
                            results.push({ href: '', title, level: null, is_group: true });
                        }
                    } else {
                        const href = el.getAttribute('href') || '';
                        const title = el.textContent?.trim() || '';
                        if (href && title) {
                            results.push({
                                href,
                                title,
                                level: el.getAttribute('level'),
                                is_group: false,
                            });
                        }
                    }
                });

                if (results.length > 0) return results;
            }

            // 2순위: level 속성을 가진 모든 a 태그 (fallback)
            const fallbackLinks = Array.from(document.querySelectorAll('a[level]'));
            if (fallbackLinks.length > 0) {
                return fallbackLinks.map(a => ({
                    href: a.getAttribute('href') || '',
                    title: a.textContent?.trim() || '',
                    level: a.getAttribute('level'),
                    is_group: false,
                })).filter(item => item.href && item.title);
            }

            return [];
        }
    """)

    if not raw_items:
        logger.warning("TOC 링크를 찾을 수 없습니다.")

    logger.info("TOC 항목 %d개 추출됨", len(raw_items))

    # 트리 구축
    toc_tree = _build_toc_tree(raw_items)

    return toc_tree, raw_items


def get_toc_sections(flat_items: list[dict]) -> list[dict]:
    """
    TOC 평탄 목록에서 실제 섹션(문단이 있는 항목)만 추출합니다.
    level 2~5인 항목들이 실제 문단 내용을 가집니다.

    특이사항: level=1 항목 중 하위 자식이 없는 리프 노드도 포함합니다.
    - 예: "기타 참고사항" 아래의 "국제회계기준과의 관계", "이 기준서의 주요 특징", "제·개정 경과"
    - 이 항목들은 li[data-paranum] 없이 일반 HTML(h3, p, table)로 구성됨
    - 다음 항목의 level >= 2이면 현재 level=1은 자식이 있는 헤더 → 제외
    - 다음 항목의 level <= 1 또는 그룹/null이거나 목록 끝이면 → 리프 → 포함
    """
    sections = []
    # 실제 level 정보가 있는 항목만 추려서 인덱싱
    level_items = [(i, item) for i, item in enumerate(flat_items) if item.get("level") is not None and not item.get("is_group")]

    for pos, (orig_idx, item) in enumerate(level_items):
        raw_level = item.get("level")
        level = float(raw_level)
        href = item.get("href", "")

        if not href:
            continue

        title, para_range = extract_title_and_range(item.get("title", ""))

        if level >= 2:
            # 리프 체크: 다음 항목의 level이 현재보다 크면 부모(자식 있음) → 제외
            # level=1 리프 체크와 동일한 패턴 적용
            next_level = None
            if pos + 1 < len(level_items):
                next_raw = level_items[pos + 1][1].get("level")
                if next_raw is not None:
                    next_level = float(next_raw)
            is_leaf = (next_level is None or next_level <= level)
            if is_leaf:
                sections.append({
                    "href": href,
                    "title": title,
                    "level": level,
                    "paragraph_range": para_range,
                    "section_id": _make_section_id(href, title),
                    "is_free_content": False,  # li[data-paranum] 기반 파싱
                })
        elif 1 <= level < 2:
            # level=1~1.x 중 리프 노드만 포함 (다음 level_item이 level>=2이면 자식 있음 → 헤더)
            next_level = None
            if pos + 1 < len(level_items):
                next_raw = level_items[pos + 1][1].get("level")
                if next_raw is not None:
                    next_level = float(next_raw)
            is_leaf = (next_level is None or next_level < 2)
            if is_leaf:
                sections.append({
                    "href": href,
                    "title": title,
                    "level": level,
                    "paragraph_range": para_range,
                    "section_id": _make_section_id(href, title),
                    "is_free_content": True,  # 자유형식 HTML 파싱
                })

    return sections


def flatten_toc(toc_tree: list[TocItem]) -> list[TocItem]:
    """TOC 트리를 평탄화하여 순서대로 반환합니다."""
    result = []

    def _recurse(items: list[TocItem]):
        for item in items:
            result.append(item)
            _recurse(item.children)

    _recurse(toc_tree)
    return result


def build_toc_path(section_id: str, toc_tree: list[TocItem]) -> str:
    """
    섹션 ID에 해당하는 목차 경로를 파이프(|)로 결합한 문자열로 반환합니다.

    예: "mA9o3F" → "본문|적용범위"
    """
    def _find_path(items: list[TocItem], target_id: str, current_path: list[str]) -> Optional[list[str]]:
        for item in items:
            new_path = current_path + [item.title]
            if item.section_id == target_id:
                return new_path
            result = _find_path(item.children, target_id, new_path)
            if result is not None:
                return result
        return None

    path = _find_path(toc_tree, section_id, [])
    return "|".join(path) if path else ""
