"""
K-IFRS 크롤러 Pydantic 데이터 모델

기준서 크롤링 결과를 구조화하는 Pydantic v2 모델을 정의합니다.
최종 출력 JSON(output/standards/{id}.json)의 스키마와 1:1 대응됩니다.

모델 계층:
    Standard
    ├── TocItem (재귀 트리)
    └── Paragraph
        ├── CrossReference  (기준서/문단 교차참조)
        ├── QnAReference    (질의회신 연결)
        └── FootnoteReference (각주 기호)

상세 스키마: STANDARD_DATA_SCHEMA.md 참조
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class CrossReference(BaseModel):
    """
    교차 참조 모델 — 문단 내 기준서/문단 링크(.std-finder, .mundan-finder).

    type="standard": 다른 기준서 전체를 참조 (standard_number, standard_title 사용)
    type="paragraph": 특정 문단/범위를 참조 (range, paragraph_ids 사용)
    """
    type: str  # "standard" 또는 "paragraph"
    display_text: str  # 화면에 표시되는 텍스트
    # 기준서 참조 전용 필드
    standard_number: Optional[str] = None
    standard_title: Optional[str] = None  # 기준서 한글 제목 (예: "중간재무보고")
    # 문단 참조 전용 필드
    range: Optional[str] = None
    paragraph_ids: list[str] = Field(default_factory=list)  # 매핑용 베이스 문단 번호 목록


class QnAReference(BaseModel):
    """
    질의회신 참조 모델 — 문단에 연결된 KASB QnA 링크(a[href^='/qnas/']).

    section_parser._expand_qna_buttons 클릭 후 DOM에 나타나는 링크에서 추출됩니다.
    """
    qna_id: str
    title: str
    url: str
    date: str


class FootnoteReference(BaseModel):
    """
    각주 참조 모델 — 문단 본문의 <sup>(한N)</sup>, <sup>(주N)</sup> 각주 기호.

    한N: 한국채택국제회계기준 번역 관련 각주
    주N: 일반 각주
    """
    id: str            # 예: "한2", "주1"
    display_text: str  # 예: "(한2)", "(주1)"


class Paragraph(BaseModel):
    """
    문단 데이터 모델 — 크롤링의 최소 수집 단위.

    li[data-paranum] 1개가 Paragraph 1개에 대응됩니다.
    text 필드는 tooltip 텍스트가 제거된 순수 텍스트이며,
    HTML 테이블은 Markdown 형식으로 변환되어 포함됩니다.
    """
    number: str  # 문단 번호 (예: "2", "BC1", "한138.6")
    section_id: str  # 소속 섹션 ID
    section_title: str  # 소속 섹션 제목
    toc_path: str  # 목차 경로 (예: "본문/적용범위")
    text: str  # 순수 텍스트 (tooltip 텍스트 제거됨)
    html: str  # 원본 HTML
    cross_references: list[CrossReference] = Field(default_factory=list)
    qna_references: list[QnAReference] = Field(default_factory=list)
    footnote_references: list[FootnoteReference] = Field(default_factory=list)


class TocItem(BaseModel):
    """
    목차 항목 모델 — aside a[href] 1개가 TocItem 1개에 대응됩니다.

    children 필드로 재귀적 트리를 구성합니다.
    level은 사이트 a[href]의 level 속성값으로, 소수점(1.5)도 가능합니다.
    """
    level: Optional[float]  # 0~5 (1.5 등 소수점 가능), None은 특수 항목
    title: str  # 제목 (문단 범위 제외)
    section_id: str  # 섹션 식별자
    href: str  # 원본 href
    paragraph_range: Optional[str] = None  # 문단 범위 (예: "2 ~ 6")
    children: list[TocItem] = Field(default_factory=list)


class Standard(BaseModel):
    """
    기준서 전체 데이터 모델 — output/standards/{standard_id}.json의 루트 구조.

    toc는 트리 구조, paragraphs는 평탄화된 순서로 저장됩니다.
    cross_references_index와 qna_index는 빠른 역방향 조회를 위한 후처리 인덱스입니다.
    """
    standard_id: str  # 기준서 번호 (예: "1001")
    standard_type: str  # 유형 (예: "기업회계기준서")
    title: str  # 한글 제목
    url: str  # 기준서 URL
    crawled_at: str  # 크롤링 시각 (ISO 8601)
    toc: list[TocItem] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    cross_references_index: dict[str, list[str]] = Field(default_factory=dict)
    qna_index: dict[str, list[str]] = Field(default_factory=dict)


# Pydantic v2는 forward reference를 자동으로 처리하지만
# TocItem의 self-referential 필드를 위해 명시적으로 업데이트
TocItem.model_rebuild()
