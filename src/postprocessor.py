"""
K-IFRS 교차 기준서 범위 전개 포스트프로세서

크롤링 시점에 타 기준서 문단 목록이 없어 미전개된 교차참조 범위(예: "44A~44E")를
전체 크롤링 완료 후 일괄 전개합니다.

실행 예:
    .venv/bin/python3 -m src.postprocessor              # 전체 처리
    .venv/bin/python3 -m src.postprocessor --standard 1001   # 단건
    .venv/bin/python3 -m src.postprocessor --preview         # 전체 미리보기
"""

import argparse
import json
import sys
from pathlib import Path

from src.utils import expand_paragraph_ranges

# 프로젝트 루트 기준 경로
STANDARDS_DIR = Path("output/standards")


def load_all_paragraph_maps(standards_dir: Path) -> dict[str, list[str]]:
    """전체 기준서 JSON을 로드하여 {standard_id: [문단 번호 순서]} 맵을 구축합니다.

    문단 번호는 JSON의 paragraphs 배열에 등장하는 순서를 그대로 유지합니다.
    expand_paragraph_ranges()에서 인덱스 기반 슬라이싱을 사용하므로 순서가 중요합니다.
    """
    maps: dict[str, list[str]] = {}

    for json_file in sorted(standards_dir.glob("*.json")):
        # _index.json 등 메타 파일 제외
        if json_file.stem.startswith("_"):
            continue
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  경고: {json_file.name} 로드 실패 — {e}", file=sys.stderr)
            continue

        standard_id = json_file.stem  # 예: "1001"
        paragraph_numbers: list[str] = []
        for para in data.get("paragraphs", []):
            # JSON 필드명은 "number" (Pydantic 모델의 paragraph_number와 구분)
            num = para.get("number")
            if num:
                paragraph_numbers.append(num)

        maps[standard_id] = paragraph_numbers

    return maps


def expand_cross_standard_ranges(
    data: dict,
    paragraph_maps: dict[str, list[str]],
) -> list[dict]:
    """단일 기준서 dict에서 교차참조 범위를 전개합니다.

    처리 로직:
    1. paragraphs[].cross_references[] 순회
    2. type=="paragraph" & paragraph_ids에 "~" 포함 항목 탐색
    3. ref.standard_number로 paragraph_maps에서 대상 문단 목록 조회
    4. expand_paragraph_ranges(ref.paragraph_ids, target_numbers) 호출
    5. 결과가 원본과 다르면 in-place 교체 + 변경 기록 반환

    반환: [{paragraph, ref_index, original, expanded, target_standard}, ...]
    """
    changes: list[dict] = []

    for para in data.get("paragraphs", []):
        para_num = para.get("number", "?")
        cross_refs = para.get("cross_references", [])

        for ref_idx, ref in enumerate(cross_refs):
            # type이 "paragraph"이고 paragraph_ids에 "~" 포함 항목이 있는 경우만 처리
            if ref.get("type") != "paragraph":
                continue

            original_ids: list[str] = ref.get("paragraph_ids", [])
            has_range = any("~" in pid for pid in original_ids)
            if not has_range:
                continue

            target_std = ref.get("standard_number")
            if not target_std:
                continue

            # 대상 기준서 문단 맵 조회
            if target_std not in paragraph_maps:
                # 대상 기준서 미크롤링 — 건너뜀
                continue

            target_numbers = paragraph_maps[target_std]
            new_ids = expand_paragraph_ranges(original_ids, target_numbers)

            # 변경된 경우만 기록 및 교체
            if new_ids != original_ids:
                changes.append({
                    "paragraph": para_num,
                    "ref_index": ref_idx,
                    "original": original_ids,
                    "expanded": new_ids,
                    "target_standard": target_std,
                })
                ref["paragraph_ids"] = new_ids

    return changes


def process_all(
    standards_dir: Path,
    target_standard: str | None,
    preview: bool,
) -> None:
    """전체 워크플로우: 문단 맵 구축 → 각 기준서 처리 → 저장

    Args:
        standards_dir: 기준서 JSON 디렉터리 경로
        target_standard: None이면 전체 처리, 값이 있으면 해당 기준서만 처리
        preview: True면 변경 내역만 출력하고 파일에 저장하지 않음
    """
    if not standards_dir.exists():
        print(f"오류: {standards_dir} 디렉터리가 존재하지 않습니다.", file=sys.stderr)
        sys.exit(1)

    # 1단계: 전체 기준서 문단 맵 구축
    paragraph_maps = load_all_paragraph_maps(standards_dir)
    print(f"문단 맵 구축 완료: {len(paragraph_maps)}개 기준서\n")

    # 2단계: 처리 대상 기준서 파일 결정
    if target_standard:
        target_file = standards_dir / f"{target_standard}.json"
        if not target_file.exists():
            print(f"오류: {target_file} 파일이 존재하지 않습니다.", file=sys.stderr)
            sys.exit(1)
        json_files = [target_file]
    else:
        json_files = sorted(
            f for f in standards_dir.glob("*.json")
            if not f.stem.startswith("_")
        )

    # 통계 집계 변수
    total_expanded = 0
    total_missing = 0
    total_open_range = 0
    saved_files: list[str] = []

    # 3단계: 각 기준서 처리
    for json_file in json_files:
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)

        standard_id = json_file.stem

        # 교차참조 범위 전개 실행
        changes = expand_cross_standard_ranges(data, paragraph_maps)

        # 미전개 교차참조 통계 수집 (대상 미존재 vs 열린 범위 분류)
        missing_stds: set[str] = set()
        open_range_count = 0
        for para in data.get("paragraphs", []):
            for ref in para.get("cross_references", []):
                if ref.get("type") != "paragraph":
                    continue
                for pid in ref.get("paragraph_ids", []):
                    if "~" not in pid:
                        continue
                    # 열린 범위 판별: "X~" (end가 빈 문자열)
                    parts = pid.split("~", 1)
                    end = parts[1].strip()
                    if end == "":
                        open_range_count += 1
                    else:
                        # 대상 기준서 미크롤링인 경우
                        target_std = ref.get("standard_number")
                        if target_std and target_std not in paragraph_maps:
                            missing_stds.add(target_std)

        # 변경 내역 출력
        if changes:
            print(f"[{standard_id}] 교차참조 범위 전개: {len(changes)}건")
            for ch in changes:
                orig_str = ", ".join(ch["original"])
                exp_str = ", ".join(ch["expanded"])
                print(f"  {ch['paragraph']}: {orig_str} → [{exp_str}] (대상: {ch['target_standard']}호)")
            total_expanded += len(changes)
        elif missing_stds:
            print(
                f"[{standard_id}] 전개 불가: {len(missing_stds)}개 기준서 미크롤링 "
                f"({', '.join(sorted(missing_stds))})"
            )
        elif open_range_count > 0:
            # 변경 없고 미크롤링도 없는데 열린 범위만 있는 경우
            pass

        total_missing += len(missing_stds)
        total_open_range += open_range_count

        # 4단계: 변경이 있고 preview 모드가 아닐 때 저장
        if changes and not preview:
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            saved_files.append(json_file.name)

    # 5단계: 요약 출력
    print()
    if preview:
        print(f"[미리보기] 전개 예정: {total_expanded}건 (저장 안 함)")
    else:
        print(
            f"완료: {total_expanded}건 전개, "
            f"{total_missing}건 대상 미존재, "
            f"{total_open_range}건 열린 범위"
        )
        if saved_files:
            print(f"저장: {', '.join(saved_files)}")
        else:
            print("저장: 변경 없음")


def main() -> None:
    """argparse CLI 진입점"""
    parser = argparse.ArgumentParser(
        description="교차 기준서 범위 전개 포스트프로세서"
    )
    parser.add_argument(
        "--standard",
        metavar="ID",
        help="처리할 기준서 번호 (예: 1001). 미지정 시 전체 처리",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="변경 내역만 출력하고 파일에 저장하지 않음",
    )
    parser.add_argument(
        "--output-dir",
        default=str(STANDARDS_DIR),
        help=f"기준서 JSON 디렉터리 (기본값: {STANDARDS_DIR})",
    )
    args = parser.parse_args()

    process_all(
        standards_dir=Path(args.output_dir),
        target_standard=args.standard,
        preview=args.preview,
    )


if __name__ == "__main__":
    main()
