"""
QnA 크롤러 Pydantic 데이터 모델

KASB QnA API 응답을 구조화하는 Pydantic v2 모델을 정의합니다.
최종 출력 JSON(output/qnas/{docNumber}.json)의 스키마와 1:1 대응됩니다.

모델 계층:
    QnADetail           — 상세 데이터 (output/qnas/{docNumber}.json)
    ├── ContentSection  — contentHtml에서 파싱된 개별 섹션
    └── RelatedStandard — bookmarkStdParagraphs 기반 기준서-문단 매핑

    QnAListItem         — 목록 API 응답의 단일 요약 항목
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class RelatedStandard(BaseModel):
    """
    QnA가 참조하는 기준서-문단 매핑.

    API 응답의 bookmarkStdParagraphs {"1001": ["4", "38"]} 구조를 모델화합니다.
    """

    standard_number: str        # 예: "1001"
    paragraphs: list[str]       # 예: ["4", "38"]


class ContentSection(BaseModel):
    """
    contentHtml에서 파싱된 개별 섹션.

    h2/h3 태그를 경계로 분할되며, heading → text/html 매핑을 저장합니다.
    대표적 heading: "배경 및 질의", "회신", "판단근거", "참고자료"
    """

    heading: str   # 원본 h3 텍스트 (예: "배경 및 질의", "회신", "판단근거")
    text: str      # 정리된 순수 텍스트
    html: str      # 원본 HTML (섹션 내부 컨텐츠)


class QnADetail(BaseModel):
    """
    질의응답 상세 데이터 — output/qnas/{docNumber}.json의 루트 구조.

    question/answer는 VectorDB 임베딩에 최적화된 분리 저장 필드이며,
    sections는 모든 섹션의 원본 텍스트/HTML을 포함하는 전체 구조입니다.
    """

    qna_id: str                 # docNumber (예: "2020-I-KQA009")
    db_id: int                  # 내부 DB ID
    type_code: int              # 11, 12, 13, 14, 15, 24, 25
    type_name: str              # 타입 이름 (예: "K-IFRS 회계기준원")
    title: str                  # 제목
    reference: Optional[str]    # 레퍼런스 텍스트 (없으면 None)
    date: str                   # ISO 날짜 (YYYY-MM-DD)
    tags: list[str] = Field(default_factory=list)   # 색인어 목록

    # 질의/회신 분리 저장 (VectorDB 적재 대비)
    # "배경 및 질의" 등 질의 관련 섹션 텍스트를 합쳐서 저장
    question: str = ""
    # "회신" 섹션 텍스트
    answer: str = ""
    # 모든 섹션 (판단근거, 참고자료 등 포함)
    sections: list[ContentSection] = Field(default_factory=list)

    # bookmarkStdParagraphs 기반 기준서-문단 매핑
    related_standards: list[RelatedStandard] = Field(default_factory=list)

    # tagSimilarDocNumbers: 유사 QnA 목록
    similar_qna_ids: list[str] = Field(default_factory=list)

    # 각주 (구조가 다양하므로 dict 그대로 보존)
    footnotes: list[dict] = Field(default_factory=list)

    crawled_at: str             # ISO 8601 (UTC)


class QnAListItem(BaseModel):
    """
    목록 API 응답의 단일 QnA 요약 항목.

    collect_all_qna_ids에서 전체 docNumber 목록 수집 시 사용하는 경량 모델로,
    상세 API 호출 전 페이지네이션 단계에서만 사용됩니다.
    """

    qna_id: str         # docNumber
    db_id: int          # id
    type_code: int      # type
    title: str          # title
    date: str           # YYYY-MM-DD
