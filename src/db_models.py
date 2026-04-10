"""
K-IFRS PostgreSQL ORM 모델 정의

테이블 구성:
    standards   — 기준서 마스터 (64개)
    paragraphs  — 문단 (메인 검색 단위, standard_id + paragraph_number로 조회)
    toc_items   — 목차 (트리를 평탄화, parent_section_id로 계층 표현)

paragraph_range 파싱 규칙:
    "2 ~ 6"     → from_="2",  to="6"
    "1"         → from_="1",  to="1"   (단일 값은 from=to)
    "BC1 ~ BC12"→ from_="BC1", to="BC12"
    null / 텍스트 → from_=None, to=None  (원본 문자열은 항상 보존)
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime

from sqlalchemy import (
    String, Text, Float, Integer, DateTime, UniqueConstraint, Index,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db_config import Base


# ---------------------------------------------------------------------------
# 유틸: paragraph_range 파싱
# ---------------------------------------------------------------------------

def parse_paragraph_range(range_str: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    paragraph_range 문자열을 (from, to) 튜플로 파싱합니다.

    Args:
        range_str: 예) "2 ~ 6", "BC1", "한2.1 ~ 한2.5", null

    Returns:
        (from_val, to_val): 파싱 성공 시 문자열 튜플, 실패 시 (None, None)
    """
    if not range_str:
        return None, None

    # " ~ " 구분자가 있으면 범위로 분리
    if " ~ " in range_str:
        parts = range_str.split(" ~ ", 1)
        from_val = parts[0].strip()
        to_val = parts[1].strip()
        # 파싱된 값이 비어있으면 None 처리
        return (from_val or None, to_val or None)

    # 하이픈(-) 구분 범위: "109-110" 형태 (숫자-숫자만 허용)
    if "-" in range_str:
        parts = range_str.split("-", 1)
        # 양쪽이 모두 숫자로만 구성된 경우에만 분리
        if parts[0].strip().isdigit() and parts[1].strip().isdigit():
            return (parts[0].strip(), parts[1].strip())

    # 단일 값: 숫자, 접두사+숫자, 점 표기 등
    # 순수 텍스트(한국어 포함)는 파싱 불가 → None 처리
    cleaned = range_str.strip()

    # 한국어가 포함된 경우 파싱 포기
    if any("\uac00" <= c <= "\ud7a3" for c in cleaned):
        return None, None

    # 그 외(영문자+숫자 조합, 점 표기 등)는 단일 값으로 처리
    if cleaned:
        return cleaned, cleaned

    return None, None


# ---------------------------------------------------------------------------
# ORM 모델
# ---------------------------------------------------------------------------

class Standard(Base):
    """
    기준서 마스터 테이블 (standards)

    기준서 1개당 1행. standard_id는 "1001", "2115" 형태의 문자열입니다.
    """
    __tablename__ = "standards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    standard_id: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, comment="기준서 번호 (예: '1001')")
    standard_type: Mapped[Optional[str]] = mapped_column(String(50), comment="유형 (예: '기업회계기준서')")
    title: Mapped[Optional[str]] = mapped_column(String(200), comment="한글 제목")
    url: Mapped[Optional[str]] = mapped_column(Text, comment="원본 URL")
    crawled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="크롤링 시각")

    # 관계 (cascade delete)
    paragraphs: Mapped[list[Paragraph]] = relationship(
        "Paragraph", back_populates="standard", cascade="all, delete-orphan"
    )
    toc_items: Mapped[list[TocItem]] = relationship(
        "TocItem", back_populates="standard", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Standard {self.standard_id} '{self.title}'>"


class Paragraph(Base):
    """
    문단 테이블 (paragraphs)

    메인 검색 단위. (standard_id, paragraph_number) 복합 유니크 제약이 걸립니다.
    교차참조/질의회신/각주는 JSONB 컬럼에 배열로 저장합니다.
    """
    __tablename__ = "paragraphs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    standard_id: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("standards.standard_id", ondelete="CASCADE"),
        nullable=False,
        comment="기준서 번호",
    )
    paragraph_number: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="문단 번호 (예: '4', 'BC1', '한2.1')"
    )
    section_id: Mapped[Optional[str]] = mapped_column(String(50), comment="소속 섹션 ID")
    section_title: Mapped[Optional[str]] = mapped_column(String(200), comment="소속 섹션 제목")
    toc_path: Mapped[Optional[str]] = mapped_column(Text, comment="목차 경로 (예: '본문|적용범위')")
    text: Mapped[Optional[str]] = mapped_column(Text, comment="순수 텍스트")
    html: Mapped[Optional[str]] = mapped_column(Text, comment="원본 HTML")
    # JSONB 컬럼: 배열 형태로 저장 (빈 배열 = [])
    cross_references: Mapped[Optional[list]] = mapped_column(JSONB, comment="교차참조 배열")
    qna_references: Mapped[Optional[list]] = mapped_column(JSONB, comment="질의회신 참조 배열")
    footnote_references: Mapped[Optional[list]] = mapped_column(JSONB, comment="각주 참조 배열")

    # 관계
    standard: Mapped[Standard] = relationship("Standard", back_populates="paragraphs")

    __table_args__ = (
        # (standard_id, paragraph_number) 복합 유니크
        UniqueConstraint("standard_id", "paragraph_number", name="uq_paragraph_std_num"),
        # 검색용 인덱스
        Index("ix_paragraphs_standard_id", "standard_id"),
        Index("ix_paragraphs_paragraph_number", "paragraph_number"),
    )

    def __repr__(self) -> str:
        return f"<Paragraph {self.standard_id}#{self.paragraph_number}>"


class TocItem(Base):
    """
    목차 테이블 (toc_items)

    JSON의 재귀 트리를 평탄화하여 저장합니다.
    parent_section_id + sort_order로 계층 탐색이 가능합니다.
    paragraph_range는 원본 문자열(paragraph_range)과
    파싱된 시작/종료(paragraph_range_from, paragraph_range_to)를 함께 저장합니다.
    """
    __tablename__ = "toc_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    standard_id: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("standards.standard_id", ondelete="CASCADE"),
        nullable=False,
        comment="기준서 번호",
    )
    section_id: Mapped[Optional[str]] = mapped_column(String(50), comment="섹션 고유 식별자")
    title: Mapped[Optional[str]] = mapped_column(String(300), comment="섹션 제목")
    level: Mapped[Optional[float]] = mapped_column(Float, comment="계층 깊이 (0~5, null=특수)")
    href: Mapped[Optional[str]] = mapped_column(Text, comment="원본 href")
    # paragraph_range 원본 + 파싱 결과
    paragraph_range: Mapped[Optional[str]] = mapped_column(String(100), comment="원본 문단 범위 문자열")
    paragraph_range_from: Mapped[Optional[str]] = mapped_column(String(30), comment="파싱된 시작 문단")
    paragraph_range_to: Mapped[Optional[str]] = mapped_column(String(30), comment="파싱된 종료 문단")
    # 트리 계층 표현
    parent_section_id: Mapped[Optional[str]] = mapped_column(String(50), comment="부모 섹션 ID (null이면 최상위)")
    sort_order: Mapped[Optional[int]] = mapped_column(Integer, comment="동일 부모 내 정렬 순서")

    # 관계
    standard: Mapped[Standard] = relationship("Standard", back_populates="toc_items")

    __table_args__ = (
        Index("ix_toc_items_standard_id", "standard_id"),
        Index("ix_toc_items_section_id", "section_id"),
    )

    def __repr__(self) -> str:
        return f"<TocItem {self.standard_id} '{self.title}' lv={self.level}>"
