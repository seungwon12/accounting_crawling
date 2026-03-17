"""
QnA 응답 파서

KASB QnA API의 상세 응답을 QnADetail 모델로 변환합니다.

주요 변환:
    - parse_content_html: contentHtml → ContentSection 목록
      h2/h3 태그 기준으로 섹션을 분할하고 number-content 구조에서 텍스트 추출
    - _classify_sections: 섹션 heading 키워드로 question/answer 분류
    - parse_related_standards: bookmarkStdParagraphs dict → RelatedStandard 목록
    - parse_tags: 콤마 구분 문자열 또는 리스트 → 태그 문자열 목록
    - parse_qna_detail: 상세 API 응답 dict → QnADetail 모델 (최종 변환 진입점)
    - parse_list_item: 목록 API 응답의 단일 항목 → QnAListItem 모델
"""
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from src.qna_config import ANSWER_HEADINGS, QNA_TYPE_MAP, QUESTION_HEADINGS
from src.qna_models import ContentSection, QnADetail, QnAListItem, RelatedStandard
from src.utils import clean_text, logger


def parse_content_html(content_html: str) -> list[ContentSection]:
    """
    contentHtml을 파싱하여 섹션 목록으로 변환합니다.

    구조 예시:
        <section>
          <h3>배경 및 질의</h3>
          <p class='number-content'>...</p>
          <h3>회신</h3>
          <p class='number-content'>...</p>
          <h3>판단근거</h3>
          ...
        </section>
        <h2>참고자료</h2>
        <section>
          <h3>질의자의 의문사항</h3>
          ...
        </section>
    """
    if not content_html or not content_html.strip():
        return []

    soup = BeautifulSoup(content_html, "html.parser")
    sections: list[ContentSection] = []

    # h3 태그를 기준으로 섹션을 분할합니다
    # h2 태그도 섹션 경계로 취급 (예: "참고자료")
    all_headings = soup.find_all(["h2", "h3"])

    for heading_tag in all_headings:
        heading_text = heading_tag.get_text(strip=True)
        if not heading_text:
            continue

        # 이 heading 이후부터 다음 h2/h3 전까지의 요소들 수집
        content_tags = []
        sibling = heading_tag.find_next_sibling()
        while sibling is not None:
            # 다음 섹션 헤딩을 만나면 중지
            if sibling.name in ("h2", "h3"):
                break
            content_tags.append(sibling)
            sibling = sibling.find_next_sibling()

        # 섹션 내부 HTML 조합
        section_html = "".join(str(tag) for tag in content_tags)

        # 순수 텍스트 추출: number-content div 구조 처리
        # <p class='number-content'><div>번호</div><div>내용</div></p>
        # → 번호를 제외하고 내용만 추출
        section_text = _extract_section_text(content_tags)

        sections.append(ContentSection(
            heading=heading_text,
            text=section_text,
            html=section_html,
        ))

    return sections


def _extract_section_text(tags: list) -> str:
    """
    섹션 내부 태그들에서 순수 텍스트를 추출합니다.

    number-content 패턴(<p class='number-content'><div>번호</div><div>내용</div></p>)에서
    첫 번째 div(항목 번호)를 제외하고 내용 텍스트만 수집합니다.
    최종 텍스트는 clean_text로 공백/개행을 정리합니다.

    Args:
        tags: BeautifulSoup Tag 객체 목록 (heading 이후 ~ 다음 heading 이전).

    Returns:
        정리된 순수 텍스트. 텍스트가 없으면 빈 문자열.
    """
    text_parts: list[str] = []

    for tag in tags:
        if not isinstance(tag, Tag):
            continue

        # number-content 패턴: <p class='number-content'><div>번호</div><div>내용</div></p>
        if "number-content" in tag.get("class", []):
            divs = tag.find_all("div", recursive=False)
            if len(divs) >= 2:
                # 첫 번째 div는 번호, 나머지가 내용
                content_divs = divs[1:]
                for div in content_divs:
                    t = div.get_text(separator=" ", strip=True)
                    if t:
                        text_parts.append(t)
            else:
                # div 구조가 아닌 경우 전체 텍스트
                t = tag.get_text(separator=" ", strip=True)
                if t:
                    text_parts.append(t)
        else:
            # 일반 태그
            t = tag.get_text(separator=" ", strip=True)
            if t:
                text_parts.append(t)

    raw = " ".join(text_parts)
    return clean_text(raw)


def _classify_sections(
    sections: list[ContentSection],
) -> tuple[str, str]:
    """
    섹션 목록에서 question과 answer 텍스트를 추출합니다.

    qna_config의 QUESTION_HEADINGS/ANSWER_HEADINGS 키워드로 각 섹션의
    heading을 분류하여 텍스트를 합칩니다.

    Args:
        sections: parse_content_html이 반환한 ContentSection 목록.

    Returns:
        (question, answer) 텍스트 튜플. 해당 섹션이 없으면 빈 문자열.
        - question: QUESTION_HEADINGS 해당 섹션 텍스트를 "\\n\\n"으로 합친 것
        - answer: ANSWER_HEADINGS 해당 섹션 텍스트를 "\\n\\n"으로 합친 것
    """
    question_parts: list[str] = []
    answer_parts: list[str] = []

    for section in sections:
        heading_lower = section.heading.strip()

        # 질의 섹션 판별
        is_question = any(kw in heading_lower for kw in QUESTION_HEADINGS)
        # 회신 섹션 판별 (정확히 "회신" 또는 "회신"으로 시작)
        is_answer = any(
            heading_lower == kw or heading_lower.startswith(kw)
            for kw in ANSWER_HEADINGS
        )

        if is_question and section.text:
            question_parts.append(section.text)
        if is_answer and section.text:
            answer_parts.append(section.text)

    return "\n\n".join(question_parts), "\n\n".join(answer_parts)


def parse_related_standards(bookmark: dict) -> list[RelatedStandard]:
    """
    bookmarkStdParagraphs를 RelatedStandard 목록으로 변환합니다.

    입력: {"1001": ["38", "4"], "1027": ["4"]}
    출력: [RelatedStandard(standard_number="1001", paragraphs=["38", "4"]), ...]
    """
    if not bookmark or not isinstance(bookmark, dict):
        return []

    result: list[RelatedStandard] = []
    for standard_number, paragraphs in bookmark.items():
        # 문단 번호가 리스트인지 확인
        if isinstance(paragraphs, list):
            para_list = [str(p) for p in paragraphs]
        else:
            para_list = [str(paragraphs)]

        result.append(RelatedStandard(
            standard_number=str(standard_number),
            paragraphs=para_list,
        ))

    return result


def parse_tags(raw_tags) -> list[str]:
    """
    태그를 리스트로 변환합니다.
    - 콤마 구분 문자열: "별도재무제표, 합병" → ["별도재무제표", "합병"]
    - 리스트: 각 항목 텍스트 추출 (dict면 "name"/"text" 키 사용)
    """
    if not raw_tags:
        return []

    # 콤마 구분 문자열 처리
    if isinstance(raw_tags, str):
        return [t.strip() for t in raw_tags.split(",") if t.strip()]

    result: list[str] = []
    for tag in raw_tags:
        if isinstance(tag, str):
            t = tag.strip()
        elif isinstance(tag, dict):
            t = (tag.get("name") or tag.get("text") or str(tag)).strip()
        else:
            t = str(tag).strip()
        if t:
            result.append(t)
    return result


def parse_similar_ids(raw) -> list[str]:
    """
    tagSimilarDocNumbers를 문자열 리스트로 변환합니다.
    - 콤마 구분 문자열: "2020-I-KQA001,2020-I-KQA002" → [...]
    - 리스트: 그대로 사용
    """
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [str(s) for s in raw if s]


def parse_date(raw_date: str | None) -> str:
    """
    날짜 문자열을 YYYY-MM-DD 형식으로 정규화합니다.
    """
    if not raw_date:
        return ""

    # "2020-01-01T00:00:00" → "2020-01-01"
    if "T" in raw_date:
        return raw_date.split("T")[0]

    # 이미 YYYY-MM-DD 형태인 경우
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
    if match:
        return match.group(1)

    return raw_date


def parse_qna_detail(raw: dict, crawled_at: str) -> Optional[QnADetail]:
    """
    상세 API 응답 dict를 QnADetail 모델로 변환합니다.

    필수 필드인 docNumber가 없으면 None 반환.
    """
    doc_number = raw.get("docNumber") or raw.get("doc_number")
    if not doc_number:
        logger.warning("docNumber 없음, 스킵: %s", raw.get("id"))
        return None

    db_id = int(raw.get("id", 0))
    type_code = int(raw.get("type", 0))
    type_name = QNA_TYPE_MAP.get(type_code, f"타입{type_code}")
    title = (raw.get("title") or "").strip()
    reference = raw.get("reference") or None
    date = parse_date(raw.get("date") or raw.get("pubDate") or "")
    tags = parse_tags(raw.get("tags") or raw.get("tag"))
    similar_ids = parse_similar_ids(raw.get("tagSimilarDocNumbers"))
    footnotes = raw.get("footnotes") or []
    bookmark = raw.get("bookmarkStdParagraphs") or {}

    # contentHtml 파싱
    content_html = raw.get("contentHtml") or ""
    sections = parse_content_html(content_html)

    # question / answer 분류
    question, answer = _classify_sections(sections)

    # contentHtml 없는 경우 fullContent fallback
    if not question and not answer:
        full_content = raw.get("fullContent") or raw.get("content") or ""
        if full_content:
            question = clean_text(full_content)

    related = parse_related_standards(bookmark)

    return QnADetail(
        qna_id=doc_number,
        db_id=db_id,
        type_code=type_code,
        type_name=type_name,
        title=title,
        reference=reference,
        date=date,
        tags=tags,
        question=question,
        answer=answer,
        sections=sections,
        related_standards=related,
        similar_qna_ids=[str(s) for s in similar_ids],
        footnotes=footnotes if isinstance(footnotes, list) else [],
        crawled_at=crawled_at,
    )


def parse_list_item(raw: dict) -> Optional[QnAListItem]:
    """
    목록 API 응답의 단일 항목을 QnAListItem으로 변환합니다.
    """
    doc_number = raw.get("docNumber") or raw.get("doc_number")
    if not doc_number:
        return None

    return QnAListItem(
        qna_id=str(doc_number),
        db_id=int(raw.get("id", 0)),
        type_code=int(raw.get("type", 0)),
        title=(raw.get("title") or "").strip(),
        date=parse_date(raw.get("date") or raw.get("pubDate") or ""),
    )
