"""
체크포인트 관리 - 진행상황 저장 및 복원

기준서 크롤링 진행상황을 JSON 파일로 저장하고 복원하여,
중단 후 --resume 옵션으로 이어서 실행할 수 있게 합니다.

체크포인트 파일 구조 (checkpoints/progress.json)::

    {
        "completed_standards": ["1001", "1002", ...],
        "current_standard": "1003",
        "current_section": "mA9o3F",
        "failed_standards": {"1012": "페이지 이동 실패"}
    }

주요 클래스:
    - CheckpointManager: 완료/실패/현재 처리 중 상태를 관리
    - check_output_exists: 출력 파일 존재 여부로 빠른 완료 확인
"""
import json
import os
from pathlib import Path
from typing import Optional

from src.utils import logger

DEFAULT_CHECKPOINT_FILE = Path("checkpoints/progress.json")


class CheckpointManager:
    """
    크롤링 진행상황을 JSON 파일로 저장/복원합니다.

    구조:
    {
        "completed_standards": ["1001", "1002", ...],
        "current_standard": "1003",
        "current_section": "mA9o3F",
        "failed_standards": {"1012": "페이지 이동 실패", "1019": "섹션 파싱 오류: ..."}
    }
    """

    def __init__(self, checkpoint_path: Path = DEFAULT_CHECKPOINT_FILE):
        self.checkpoint_path = checkpoint_path
        self._data: dict = self._load()

    def _load(self) -> dict:
        """체크포인트 파일을 로드합니다."""
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info("체크포인트 로드됨: %s", self.checkpoint_path)
                logger.info(
                    "완료된 기준서: %d개",
                    len(data.get("completed_standards", []))
                )
                return data
            except Exception as e:
                logger.warning("체크포인트 로드 실패, 새로 시작: %s", e)
        return {"completed_standards": [], "current_standard": None, "current_section": None}

    def _save(self) -> None:
        """체크포인트 파일을 저장합니다."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("체크포인트 저장 실패: %s", e)

    def is_standard_completed(self, standard_id: str) -> bool:
        """기준서가 이미 완료되었는지 확인합니다."""
        return standard_id in self._data.get("completed_standards", [])

    def mark_standard_completed(self, standard_id: str) -> None:
        """기준서를 완료로 표시합니다. 실패 기록이 있으면 자동 제거합니다."""
        completed = self._data.setdefault("completed_standards", [])
        if standard_id not in completed:
            completed.append(standard_id)
        self._data["current_standard"] = None
        self._data["current_section"] = None
        # 재시도 성공 시 실패 기록 제거
        failed = self._data.get("failed_standards", {})
        failed.pop(standard_id, None)
        self._save()
        logger.info("기준서 %s 완료 표시됨", standard_id)

    def mark_standard_failed(self, standard_id: str, reason: str) -> None:
        """기준서를 실패로 기록합니다."""
        failed = self._data.setdefault("failed_standards", {})
        failed[standard_id] = reason
        self._save()
        logger.warning("기준서 %s 실패 기록됨: %s", standard_id, reason)

    def get_failed_standards(self) -> dict[str, str]:
        """실패한 기준서 목록을 반환합니다. {standard_id: reason}"""
        return self._data.get("failed_standards", {})

    def clear_failed(self, standard_id: str) -> None:
        """특정 기준서의 실패 기록을 제거합니다 (재시도 성공 후 호출)."""
        failed = self._data.get("failed_standards", {})
        if standard_id in failed:
            failed.pop(standard_id)
            self._save()

    def set_current(self, standard_id: str, section_id: Optional[str] = None) -> None:
        """현재 처리 중인 기준서와 섹션을 기록합니다."""
        self._data["current_standard"] = standard_id
        self._data["current_section"] = section_id
        self._save()

    def get_current_standard(self) -> Optional[str]:
        """현재 처리 중인 기준서 ID를 반환합니다."""
        return self._data.get("current_standard")

    def get_completed_standards(self) -> list[str]:
        """완료된 기준서 목록을 반환합니다."""
        return self._data.get("completed_standards", [])

    def reset(self) -> None:
        """체크포인트를 초기화합니다."""
        self._data = {
            "completed_standards": [],
            "current_standard": None,
            "current_section": None,
            "failed_standards": {},
        }
        self._save()
        logger.info("체크포인트 초기화됨")


def check_output_exists(standard_id: str, output_dir: Path) -> bool:
    """
    출력 디렉토리에 기준서 JSON이 이미 존재하는지 확인합니다.
    재시작 시 이미 완료된 기준서를 스킵하는 데 사용됩니다.
    """
    output_file = output_dir / f"{standard_id}.json"
    return output_file.exists()
