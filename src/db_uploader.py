"""
K-IFRS 기준서 JSON → PostgreSQL 적재 스크립트

사용법:
    .venv/bin/python3 -m src.db_uploader              # 전체 64개 기준서 적재
    .venv/bin/python3 -m src.db_uploader --standard 1001  # 단건 적재
    .venv/bin/python3 -m src.db_uploader --drop        # 테이블 초기화 후 전체 적재

동작:
    1. output/standards/*.json 파일을 읽는다.
    2. standards 테이블에 기준서 정보를 upsert한다.
    3. paragraphs 테이블에 문단을 bulk insert한다.
    4. toc_items 테이블에 TOC 트리를 평탄화하여 bulk insert한다.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db_config import SessionLocal, init_db, drop_and_recreate_db
from src.db_models import Standard, Paragraph, TocItem, parse_paragraph_range

# 로거 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STANDARDS_DIR = Path(__file__).parent.parent / "output" / "standards"


# ---------------------------------------------------------------------------
# TOC 트리 평탄화
# ---------------------------------------------------------------------------

def flatten_toc(
    toc_items: list[dict],
    standard_id: str,
    parent_section_id: Optional[str] = None,
    sort_counter: Optional[list[int]] = None,
) -> list[dict]:
    """
    재귀적 TOC 트리를 평탄화하여 DB 삽입용 dict 리스트를 반환합니다.

    Args:
        toc_items: JSON의 toc 배열 (재귀 children 포함)
        standard_id: 기준서 번호
        parent_section_id: 부모 섹션 ID (최상위면 None)
        sort_counter: 글로벌 sort_order 카운터 (재귀 공유용)

    Returns:
        평탄화된 TocItem dict 리스트
    """
    if sort_counter is None:
        sort_counter = [0]

    result = []
    for item in toc_items:
        range_str = item.get("paragraph_range")
        from_val, to_val = parse_paragraph_range(range_str)

        row = {
            "standard_id": standard_id,
            "section_id": item.get("section_id"),
            "title": item.get("title"),
            "level": item.get("level"),
            "href": item.get("href"),
            "paragraph_range": range_str,
            "paragraph_range_from": from_val,
            "paragraph_range_to": to_val,
            "parent_section_id": parent_section_id,
            "sort_order": sort_counter[0],
        }
        sort_counter[0] += 1
        result.append(row)

        # 자식 노드 재귀 처리
        children = item.get("children", [])
        if children:
            result.extend(
                flatten_toc(
                    children,
                    standard_id,
                    parent_section_id=item.get("section_id"),
                    sort_counter=sort_counter,
                )
            )

    return result


# ---------------------------------------------------------------------------
# 단일 기준서 처리
# ---------------------------------------------------------------------------

def upsert_standard(session, data: dict) -> None:
    """
    기준서 마스터(standards)를 upsert합니다.
    이미 존재하면 title, standard_type, url, crawled_at을 업데이트합니다.
    """
    crawled_at = None
    raw_ts = data.get("crawled_at")
    if raw_ts:
        try:
            crawled_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            pass

    stmt = (
        pg_insert(Standard)
        .values(
            standard_id=data["standard_id"],
            standard_type=data.get("standard_type"),
            title=data.get("title"),
            url=data.get("url"),
            crawled_at=crawled_at,
        )
        .on_conflict_do_update(
            index_elements=["standard_id"],
            set_={
                "standard_type": data.get("standard_type"),
                "title": data.get("title"),
                "url": data.get("url"),
                "crawled_at": crawled_at,
            },
        )
    )
    session.execute(stmt)


def delete_existing_children(session, standard_id: str) -> tuple[int, int]:
    """
    기존 paragraphs/toc_items를 삭제하고 삭제된 건수를 반환합니다.
    (upsert 대신 delete+insert 전략: 문단 번호 변경에도 안전)
    """
    para_deleted = session.query(Paragraph).filter_by(standard_id=standard_id).delete()
    toc_deleted = session.query(TocItem).filter_by(standard_id=standard_id).delete()
    return para_deleted, toc_deleted


def insert_paragraphs(session, data: dict, standard_id: str) -> int:
    """
    paragraphs 배열을 bulk insert합니다.
    """
    rows = []
    for p in data.get("paragraphs", []):
        rows.append({
            "standard_id": standard_id,
            "paragraph_number": p["number"],
            "section_id": p.get("section_id"),
            "section_title": p.get("section_title"),
            "toc_path": p.get("toc_path"),
            "text": p.get("text"),
            "html": p.get("html"),
            "cross_references": p.get("cross_references") or [],
            "qna_references": p.get("qna_references") or [],
            "footnote_references": p.get("footnote_references") or [],
        })

    if rows:
        session.bulk_insert_mappings(Paragraph, rows)

    return len(rows)


def insert_toc_items(session, data: dict, standard_id: str) -> int:
    """
    TOC 트리를 평탄화하여 bulk insert합니다.
    """
    rows = flatten_toc(data.get("toc", []), standard_id)

    if rows:
        session.bulk_insert_mappings(TocItem, rows)

    return len(rows)


def process_standard_file(json_path: Path) -> tuple[int, int]:
    """
    단일 기준서 JSON 파일을 읽어 DB에 적재합니다.

    Returns:
        (paragraph_count, toc_count) 적재된 건수 튜플
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    standard_id = data["standard_id"]

    with SessionLocal() as session:
        # 1. standards upsert
        upsert_standard(session, data)
        session.flush()

        # 2. 기존 하위 데이터 삭제
        para_del, toc_del = delete_existing_children(session, standard_id)

        # 3. paragraphs 삽입
        para_count = insert_paragraphs(session, data, standard_id)

        # 4. toc_items 삽입
        toc_count = insert_toc_items(session, data, standard_id)

        session.commit()

    return para_count, toc_count


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="K-IFRS 기준서 JSON → PostgreSQL 적재")
    parser.add_argument(
        "--standard", type=str, default=None,
        help="단건 처리할 기준서 번호 (예: 1001). 생략 시 전체 처리."
    )
    parser.add_argument(
        "--drop", action="store_true",
        help="테이블을 초기화(drop+recreate)한 후 적재합니다."
    )
    args = parser.parse_args()

    # DB 초기화
    if args.drop:
        logger.info("테이블 초기화 (drop + recreate)...")
        drop_and_recreate_db()
    else:
        logger.info("테이블 확인 (존재하면 유지)...")
        init_db()

    # 처리할 파일 목록 결정
    if args.standard:
        target_files = [STANDARDS_DIR / f"{args.standard}.json"]
        missing = [f for f in target_files if not f.exists()]
        if missing:
            logger.error(f"파일을 찾을 수 없습니다: {missing[0]}")
            sys.exit(1)
    else:
        target_files = sorted(STANDARDS_DIR.glob("*.json"))
        if not target_files:
            logger.error(f"적재할 JSON 파일이 없습니다: {STANDARDS_DIR}")
            sys.exit(1)

    logger.info(f"적재 대상: {len(target_files)}개 기준서")

    total_para = 0
    total_toc = 0
    start = datetime.now(timezone.utc)

    for i, json_path in enumerate(target_files, 1):
        std_id = json_path.stem
        logger.info(f"[{i}/{len(target_files)}] 기준서 {std_id} 처리 중...")
        try:
            para_count, toc_count = process_standard_file(json_path)
            logger.info(f"  → 문단 {para_count}개, TOC {toc_count}개 적재 완료")
            total_para += para_count
            total_toc += toc_count
        except Exception as e:
            logger.error(f"  ✗ 기준서 {std_id} 처리 실패: {e}", exc_info=True)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(
        f"\n완료: 문단 총 {total_para}개, TOC 총 {total_toc}개 | "
        f"소요 시간: {elapsed:.1f}초"
    )


if __name__ == "__main__":
    main()
