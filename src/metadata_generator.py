"""
K-IFRS 기준서 메타데이터 생성기

크롤링 완료된 기준서 JSON에서 RAG Agent용 경량 메타데이터를 추출.
기존 크롤러와 완전히 독립적으로 실행.

실행 예:
    .venv/bin/python3 -m src.metadata_generator              # 전체 생성
    .venv/bin/python3 -m src.metadata_generator --standard 1001          # 단건
    .venv/bin/python3 -m src.metadata_generator --standard 1001 --preview  # 미리보기
"""

import argparse
import json
import sys
from pathlib import Path


# 프로젝트 루트 기준 경로
STANDARDS_DIR = Path("output/standards")
METADATA_DIR = Path("output/metadata")


def _collect_toc_keywords(nodes: list) -> list[str]:
    """TOC 트리에서 고유 키워드(title) 추출. level이 None인 항목 제외.

    경로 대신 각 노드의 title만 수집하여 중복 없는 정렬된 리스트 반환.
    RAG 검색 시 상위 노드 토큰이 과다 반복되는 문제를 방지.
    """
    keywords: set[str] = set()

    def _walk(nodes: list) -> None:
        for node in nodes:
            # level이 None인 항목(저작권 등)은 자신과 자식 모두 제외
            if node.get("level") is None:
                continue
            title = node.get("title", "").strip()
            if title:
                keywords.add(title)
            _walk(node.get("children", []))

    _walk(nodes)
    return sorted(keywords)


def _extract_section_text(paragraphs: list, section_keyword: str) -> str:
    """toc_path의 두 번째 세그먼트가 section_keyword인 문단들의 text를 연결.

    예: section_keyword="목적" → toc_path "본문|목적" 문단들 수집
    """
    texts = []
    for para in paragraphs:
        toc_path = para.get("toc_path", "")
        if not toc_path:
            continue
        segments = toc_path.split("|")
        # 두 번째 세그먼트(index 1)가 키워드와 일치하는지 확인
        if len(segments) >= 2 and segments[1] == section_keyword:
            text = para.get("text", "").strip()
            if text:
                texts.append(text)

    return " ".join(texts)


def extract_metadata(data: dict) -> dict:
    """단일 기준서 JSON → 메타데이터 딕셔너리 변환.

    Args:
        data: 기준서 JSON 전체 딕셔너리

    Returns:
        RAG Agent용 경량 메타데이터 딕셔너리
    """
    paragraphs = data.get("paragraphs", [])
    toc = data.get("toc", [])
    cross_ref_index = data.get("cross_references_index", {})

    # 목적 및 적용범위 텍스트 추출
    purpose = _extract_section_text(paragraphs, "목적")
    scope = _extract_section_text(paragraphs, "적용범위")

    # TOC 고유 키워드 추출
    toc_outline = _collect_toc_keywords(toc)

    # 참조하는 기준서 목록 (정렬)
    references_to = sorted(cross_ref_index.keys())

    return {
        "standard_id": data.get("standard_id", ""),
        "standard_type": data.get("standard_type", ""),
        "title": data.get("title", ""),
        "purpose": purpose,
        "scope": scope,
        "toc_outline": toc_outline,
        "paragraph_count": len(paragraphs),
        "references_to": references_to,
        "referenced_by": [],  # build_referenced_by()에서 채워짐
    }


def build_referenced_by(all_metadata: dict[str, dict]) -> None:
    """전체 메타데이터에서 역참조(referenced_by) 집계 후 in-place 업데이트.

    A의 references_to에 B가 있으면 → B의 referenced_by에 A 추가.

    Args:
        all_metadata: {standard_id: metadata_dict} 형태의 전체 메타데이터
    """
    # 역참조 인덱스 초기화
    referenced_by_map: dict[str, set] = {sid: set() for sid in all_metadata}

    # 모든 기준서를 순회하며 역참조 수집
    for source_id, meta in all_metadata.items():
        for target_id in meta.get("references_to", []):
            if target_id in referenced_by_map:
                referenced_by_map[target_id].add(source_id)

    # 정렬된 리스트로 변환 후 메타데이터에 반영
    for sid, meta in all_metadata.items():
        meta["referenced_by"] = sorted(referenced_by_map[sid])


def main() -> None:
    """CLI 진입점: output/standards/*.json 읽기 → 메타데이터 추출 → output/metadata/ 저장"""
    parser = argparse.ArgumentParser(
        description="K-IFRS 기준서 메타데이터 생성기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  %(prog)s                          전체 기준서 메타데이터 생성
  %(prog)s --standard 1001          1001호 단건 생성
  %(prog)s --standard 1001 --preview  1001호 미리보기 (파일 저장 없음)
        """,
    )
    parser.add_argument(
        "--standard",
        type=str,
        help="특정 기준서 번호 (예: 1001). 미지정 시 전체 처리",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="파일 저장 없이 stdout에 JSON 출력 (--standard와 함께 사용)",
    )
    args = parser.parse_args()

    # --preview는 --standard와 함께만 의미 있음
    if args.preview and not args.standard:
        print("오류: --preview는 --standard와 함께 사용해야 합니다.", file=sys.stderr)
        sys.exit(1)

    # 처리 대상 파일 목록 결정
    if args.standard:
        target_file = STANDARDS_DIR / f"{args.standard}.json"
        if not target_file.exists():
            print(f"오류: {target_file} 파일이 존재하지 않습니다.", file=sys.stderr)
            sys.exit(1)
        standard_files = [target_file]
    else:
        standard_files = sorted(STANDARDS_DIR.glob("*.json"))
        if not standard_files:
            print(f"오류: {STANDARDS_DIR}/ 에 JSON 파일이 없습니다.", file=sys.stderr)
            sys.exit(1)

    print(f"처리 대상: {len(standard_files)}개 기준서")

    # 모든 기준서 JSON 로드 및 메타데이터 추출
    all_metadata: dict[str, dict] = {}
    for f in standard_files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        meta = extract_metadata(data)
        all_metadata[meta["standard_id"]] = meta

    # referenced_by 역참조 집계
    # 단건 --preview 모드에서도 전체 파일을 로드해야 정확한 역참조를 계산할 수 있음.
    # 단건 모드이면서 전체 파일을 아직 로드하지 않은 경우 전체 로드 후 집계.
    if args.standard and not args.preview:
        # 단건 저장 모드: 역참조 정확도를 위해 전체 로드
        all_files = sorted(STANDARDS_DIR.glob("*.json"))
        for f in all_files:
            sid = f.stem
            if sid not in all_metadata:
                with open(f, encoding="utf-8") as fp:
                    data = json.load(fp)
                tmp_meta = extract_metadata(data)
                all_metadata[tmp_meta["standard_id"]] = tmp_meta

    build_referenced_by(all_metadata)

    # --preview 모드: stdout 출력 후 종료
    if args.preview:
        target_meta = all_metadata[args.standard]
        print(json.dumps(target_meta, ensure_ascii=False, indent=2))
        return

    # 메타데이터 파일 저장
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.standard:
        # 단건 저장
        save_ids = [args.standard]
    else:
        # 전체 저장
        save_ids = list(all_metadata.keys())

    for sid in save_ids:
        out_path = METADATA_DIR / f"{sid}.json"
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(all_metadata[sid], fp, ensure_ascii=False, indent=2)

    print(f"완료: {len(save_ids)}개 메타데이터 파일 → {METADATA_DIR}/")


if __name__ == "__main__":
    main()
