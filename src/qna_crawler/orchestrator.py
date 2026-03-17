"""
QnA 크롤링 오케스트레이터

목록 API 페이지네이션 → 상세 API 건별 호출 → JSON 저장 → _index.json 생성의
전체 QnA 수집 워크플로우를 관리합니다.

워크플로우 (crawl_qna):
    1. collect_all_qna_ids: 목록 API 순회로 전체 docNumber 목록 수집
    2. 재개 모드: 체크포인트 + 기존 파일 기반으로 완료된 항목 제외
    3. _crawl_single: 상세 API 호출 → parse_qna_detail → JSON 저장
    4. _save_index_json: 전체 수집 후 output/qnas/_index.json 생성

주요 클래스:
    - QnaCheckpoint: QnA 전용 체크포인트 (checkpoints/qna_progress.json)
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.qna_config import QNA_LIST_ROWS
from src.qna_crawler.api_client import QnaApiClient
from src.qna_crawler.parser import parse_list_item, parse_qna_detail
from src.qna_models import QnADetail, QnAListItem
from src.utils import logger


class QnaCheckpoint:
    """
    QnA 크롤링 체크포인트 관리.

    기존 CheckpointManager와 별도로 QnA 전용으로 구현합니다.
    구조: {"completed_ids": ["2020-I-KQA009", ...], "total_count": 2243}
    """

    def __init__(self, checkpoint_path: Path) -> None:
        self.path = checkpoint_path
        self._data = self._load()

    def _load(self) -> dict:
        """체크포인트 파일을 로드합니다. 파일이 없으면 빈 상태를 반환합니다."""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(
                    "QnA 체크포인트 로드: %d개 완료",
                    len(data.get("completed_ids", []))
                )
                return data
            except Exception as e:
                logger.warning("QnA 체크포인트 로드 실패, 새로 시작: %s", e)
        return {"completed_ids": [], "total_count": 0}

    def _save(self) -> None:
        """체크포인트 파일을 저장합니다. 부모 디렉토리가 없으면 자동 생성합니다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("QnA 체크포인트 저장 실패: %s", e)

    def is_completed(self, qna_id: str) -> bool:
        """주어진 QnA ID가 이미 완료되었는지 확인합니다."""
        return qna_id in self._data.get("completed_ids", [])

    def mark_completed(self, qna_id: str) -> None:
        """주어진 QnA ID를 완료로 표시하고 저장합니다."""
        ids = self._data.setdefault("completed_ids", [])
        if qna_id not in ids:
            ids.append(qna_id)
        self._save()

    def set_total(self, total: int) -> None:
        """전체 QnA 건수를 체크포인트에 기록합니다."""
        self._data["total_count"] = total
        self._save()

    def get_completed_ids(self) -> list[str]:
        """완료된 QnA ID 목록을 반환합니다."""
        return self._data.get("completed_ids", [])


def _save_qna_json(detail: QnADetail, output_dir: Path) -> None:
    """
    QnA 상세 데이터를 {docNumber}.json으로 저장합니다.

    Args:
        detail: 저장할 QnADetail 모델.
        output_dir: 출력 디렉토리 경로. 존재하지 않으면 자동 생성됩니다.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{detail.qna_id}.json"
    data = detail.model_dump(mode="json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.debug("저장됨: %s", output_file)


def _save_index_json(
    details: list[QnADetail],
    output_dir: Path,
) -> None:
    """
    output/qnas/_index.json을 생성합니다.
    전체 QnA 요약 인덱스 (상세 내용 제외, 빠른 검색용).
    """
    index = []
    for d in details:
        index.append({
            "qna_id": d.qna_id,
            "db_id": d.db_id,
            "type_code": d.type_code,
            "type_name": d.type_name,
            "title": d.title,
            "date": d.date,
            "tags": d.tags,
            "related_standards": [r.standard_number for r in d.related_standards],
            "similar_qna_ids": d.similar_qna_ids,
        })

    index_file = output_dir / "_index.json"
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(
            {"total": len(index), "items": index},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("인덱스 저장됨: %s (%d건)", index_file, len(index))


async def collect_all_qna_ids(client: QnaApiClient) -> list[str]:
    """
    목록 API를 순회하여 전체 QnA docNumber 목록을 수집합니다.

    반환: ["2020-I-KQA009", "2019-I-KQA001", ...]
    """
    all_ids: list[str] = []
    page = 0

    logger.info("QnA 목록 수집 시작...")

    while True:
        data = await client.fetch_qna_list(page=page, rows=QNA_LIST_ROWS)

        if not data:
            logger.warning("목록 API 응답 없음 (page=%d)", page)
            break

        # 전체 건수 확인
        total_count = data.get("totalCount") or data.get("total") or 0
        items_raw = data.get("items") or data.get("data") or []

        if not items_raw:
            logger.info("더 이상 항목 없음 (page=%d)", page)
            break

        for raw in items_raw:
            item = parse_list_item(raw)
            if item:
                all_ids.append(item.qna_id)

        logger.info(
            "목록 수집: page=%d, %d건 추가 (누적 %d건 / 전체 %d건)",
            page, len(items_raw), len(all_ids), total_count
        )

        # 마지막 페이지 확인
        if len(items_raw) < QNA_LIST_ROWS or len(all_ids) >= total_count > 0:
            break

        page += 1

    logger.info("목록 수집 완료: 총 %d건", len(all_ids))
    return all_ids


async def crawl_qna(
    output_dir: Path,
    checkpoint_path: Path,
    target_qna_id: Optional[str] = None,
    resume: bool = False,
) -> None:
    """
    전체 QnA 수집 워크플로우.

    Args:
        output_dir: JSON 저장 디렉토리 (output/qnas)
        checkpoint_path: 체크포인트 파일 경로
        target_qna_id: 단건 수집 시 docNumber (None이면 전체)
        resume: True이면 체크포인트에서 재개
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = QnaCheckpoint(checkpoint_path)

    async with QnaApiClient() as client:
        # 단건 모드
        if target_qna_id:
            logger.info("단건 수집: %s", target_qna_id)
            await _crawl_single(client, target_qna_id, output_dir, checkpoint)
            return

        # 1단계: 전체 docNumber 목록 수집
        all_ids = await collect_all_qna_ids(client)
        checkpoint.set_total(len(all_ids))

        # 2단계: 재개 모드 - 완료된 항목 제외
        if resume:
            completed = set(checkpoint.get_completed_ids())
            # 출력 파일이 존재하는 경우도 스킵 (파일 기반 재개)
            existing = {
                p.stem for p in output_dir.glob("*.json") if p.stem != "_index"
            }
            skip_ids = completed | existing
            pending = [qid for qid in all_ids if qid not in skip_ids]
            logger.info(
                "재개 모드: 전체 %d건, 완료 %d건, 남은 %d건",
                len(all_ids), len(skip_ids), len(pending)
            )
        else:
            pending = all_ids

        # 3단계: 상세 API 호출 및 저장
        success = 0
        fail = 0
        all_details: list[QnADetail] = []

        for i, qna_id in enumerate(pending):
            logger.info(
                "[%d/%d] 수집 중: %s",
                i + 1, len(pending), qna_id
            )
            detail = await _crawl_single(client, qna_id, output_dir, checkpoint)
            if detail:
                success += 1
                all_details.append(detail)
            else:
                fail += 1

        logger.info("수집 완료: 성공 %d건, 실패 %d건", success, fail)

        # 4단계: resume 모드에서 기존 파일의 detail도 인덱스에 포함
        if resume:
            # 이미 저장된 JSON 파일에서 로드
            existing_files = [
                p for p in output_dir.glob("*.json") if p.stem != "_index"
            ]
            loaded_ids = {d.qna_id for d in all_details}
            for json_file in existing_files:
                if json_file.stem not in loaded_ids:
                    try:
                        with open(json_file, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        detail = QnADetail.model_validate(raw)
                        all_details.append(detail)
                    except Exception as e:
                        logger.warning("기존 파일 로드 실패: %s - %s", json_file, e)

        # 5단계: _index.json 생성
        if all_details:
            _save_index_json(all_details, output_dir)


async def _crawl_single(
    client: QnaApiClient,
    qna_id: str,
    output_dir: Path,
    checkpoint: QnaCheckpoint,
) -> Optional[QnADetail]:
    """
    단일 QnA를 수집하고 저장합니다.
    """
    crawled_at = datetime.now(timezone.utc).isoformat()

    try:
        raw = await client.fetch_qna_detail(qna_id)
        if not raw:
            logger.warning("상세 데이터 없음: %s", qna_id)
            return None

        detail = parse_qna_detail(raw, crawled_at)
        if not detail:
            logger.warning("파싱 실패: %s", qna_id)
            return None

        _save_qna_json(detail, output_dir)
        checkpoint.mark_completed(qna_id)

        # 로그: 질의/회신 추출 상태
        logger.debug(
            "%s: 섹션 %d개, question=%d자, answer=%d자, related=%d개",
            qna_id,
            len(detail.sections),
            len(detail.question),
            len(detail.answer),
            len(detail.related_standards),
        )
        return detail

    except Exception as e:
        logger.error("수집 실패: %s - %s", qna_id, e)
        return None
