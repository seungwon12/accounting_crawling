"""
유틸리티 함수: 로깅 설정, 텍스트 정리

크롤러 전체에서 공통으로 사용하는 헬퍼 함수를 제공합니다.

주요 기능:
    - setup_logging: 단일 콘솔 핸들러 로거 설정 (중복 핸들러 방지)
    - clean_text: HTML 추출 텍스트 정리 (Markdown 테이블 보존, 공백 압축)
    - extract_title_and_range: TOC 텍스트에서 제목/문단 범위 분리
    - extract_standard_number: 기준서 표시 텍스트에서 번호 추출
    - resolve_paragraph_ids: 복합 문단 참조 표기를 베이스 번호 목록으로 변환
    - expand_paragraph_ranges: 범위 표기(start~end)를 문서 순서 기반으로 전개
"""
import logging
import re
import sys
from typing import Optional


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """로거를 설정하고 반환합니다."""
    logger = logging.getLogger("kifrs_crawler")
    if logger.handlers:
        return logger  # 이미 설정됨

    logger.setLevel(level)

    # 콘솔 핸들러 설정
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # 포맷터 설정
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


# 모듈 레벨 로거
logger = setup_logging()


def extract_title_and_range(raw_title: str) -> tuple[str, Optional[str]]:
    """
    TOC 항목 텍스트에서 제목과 문단 범위를 분리합니다.

    예시:
        "적용범위(2 ~ 6)" → ("적용범위", "2 ~ 6")
        "본문" → ("본문", None)
        "￭ 일반사항(15 ~ 46)" → ("일반사항", "15 ~ 46")
    """
    # 제목 앞의 불릿/특수 기호 제거 (한글/영문/숫자/따옴표/괄호 등 실제 텍스트 문자가 나올 때까지)
    raw_title = re.sub(r"^[^\w'\"(]+", "", raw_title.strip())
    # 마지막 괄호 쌍에서 문단 범위 추출
    # 괄호 안에 숫자, ~, 공백, 영문, 한글(한) 등이 올 수 있음
    pattern = r"^(.+?)\(([^)]+)\)\s*$"
    match = re.search(pattern, raw_title.strip())
    if match:
        title = match.group(1).strip()
        para_range = match.group(2).strip()
        # 문단 범위인지 확인 (순수 숫자, BC접두사, 한접두사 등)
        range_pattern = r"^[0-9A-Za-z가-힣~\s\.\-]+$"
        if re.match(range_pattern, para_range):
            return title, para_range
    return raw_title.strip(), None


def clean_text(text: str) -> str:
    """
    HTML에서 추출한 텍스트를 정리합니다.
    - Markdown 테이블 행(|로 시작): 개행 유지
    - 일반 텍스트: 개행을 공백으로 변환 (인라인 div 주변 과도한 개행 제거)
    - 줄 내부의 탭/연속 공백을 단일 공백으로 압축
    - 앞뒤 공백 제거
    """
    text = text.replace("\r", "")
    lines = text.split("\n")
    # 각 줄 내부 탭 및 연속 공백 정리
    cleaned = [re.sub(r" {2,}", " ", re.sub(r"\t+", " ", l)).strip() for l in lines]

    # 테이블 블록(|로 시작)과 비테이블 블록으로 분류
    blocks: list[tuple[bool, list[str]]] = []
    current_lines: list[str] = []
    current_is_table = False

    for line in cleaned:
        if not line:
            # 빈 줄: 현재 블록에 그대로 추가 (블록 전환 없음)
            current_lines.append(line)
            continue
        is_table = line.startswith("|")
        if not current_lines:
            current_is_table = is_table
        elif is_table != current_is_table:
            # 블록 유형이 바뀌면 저장 후 새 블록 시작
            blocks.append((current_is_table, current_lines))
            current_lines = []
            current_is_table = is_table
        current_lines.append(line)

    if current_lines:
        blocks.append((current_is_table, current_lines))

    # 블록별 처리: 테이블→개행 유지, 비테이블→공백 변환
    parts: list[str] = []
    for is_table, blines in blocks:
        if is_table:
            # 테이블 행은 개행 유지 (빈 줄 제거)
            t = "\n".join(l for l in blines if l)
        else:
            # 일반 텍스트는 빈 줄 제거 후 공백으로 합침
            t = " ".join(l for l in blines if l)
            t = re.sub(r" {2,}", " ", t)
        if t:
            parts.append(t)

    return "\n\n".join(parts).strip()


def extract_standard_number(display_text: str) -> Optional[str]:
    """
    기준서 표시 텍스트에서 번호를 추출합니다.

    예시:
        "기업회계기준서 제1034호" → "1034"
        "기업회계기준해석서 제2101호" → "2101"
    """
    match = re.search(r"제(\d+)호", display_text)
    if match:
        return match.group(1)
    return None


def extract_paragraph_range(range_text: str) -> Optional[str]:
    """
    문단 참조 텍스트에서 범위를 추출합니다.

    예시:
        "문단 15~35" → "15~35"
        "문단 15" → "15"
    """
    match = re.search(r"문단\s*(.+)", range_text.strip())
    if match:
        return match.group(1).strip()
    return range_text.strip()


def expand_paragraph_ranges(
    paragraph_ids: list[str],
    all_paragraph_numbers: list[str],
) -> list[str]:
    """
    paragraph_ids 목록에서 `start~end` 범위 패턴을 문서 순서 기반으로 전개합니다.

    문단 번호에 BC1, 40A, IG5A 등 비숫자 패턴이 존재하므로,
    파싱된 전체 문단 목록의 인덱스를 기준으로 슬라이싱합니다.

    예시:
        paragraph_ids=["IG3~IG6"], all_paragraph_numbers=[..., "IG3", "IG4", "IG5", "IG5A", "IG6", ...]
        → ["IG3", "IG4", "IG5", "IG5A", "IG6"]

    타 기준서 참조(start 또는 end가 현재 기준서에 없음) → 원본 문자열 유지 (폴백)
    중복은 순서를 유지하면서 제거합니다.
    """
    result: list[str] = []
    seen: set[str] = set()

    for item in paragraph_ids:
        if "~" not in item:
            # 범위 아님 — 그대로 추가
            if item not in seen:
                result.append(item)
                seen.add(item)
            continue

        # 범위 분리: "15~35" → start="15", end="35"
        parts = item.split("~", 1)
        start, end = parts[0].strip(), parts[1].strip()

        # 양쪽 모두 현재 기준서 문단 목록에 존재하는지 확인
        if start in all_paragraph_numbers and end in all_paragraph_numbers:
            start_idx = all_paragraph_numbers.index(start)
            end_idx = all_paragraph_numbers.index(end)
            # 정방향 슬라이스 (start_idx <= end_idx 보장)
            if start_idx <= end_idx:
                for num in all_paragraph_numbers[start_idx:end_idx + 1]:
                    if num not in seen:
                        result.append(num)
                        seen.add(num)
            else:
                # 역방향 참조는 원본 유지
                if item not in seen:
                    result.append(item)
                    seen.add(item)
        else:
            # 타 기준서 참조 — 원본 유지 (폴백)
            if item not in seen:
                result.append(item)
                seen.add(item)

    return result


def resolve_paragraph_ids(range_str: str) -> list[str]:
    """
    문단 range 문자열에서 매핑 가능한 베이스 문단 번호 목록을 반환합니다.

    서브아이템 표기 `(숫자)`, `(한글)` 를 제거하고 복합 참조를 분리합니다.

    예시:
        "3(1)"           → ["3"]
        "106(4)(나)"     → ["106"]
        "6.5.11(4)"      → ["6.5.11"]
        "20(3)과 (4)"    → ["20"]
        "69(4) 및 106"   → ["69", "106"]
        "15~35"          → ["15~35"]
        "15"             → ["15"]
        "40A(1)"         → ["40A"]
    """
    if not range_str:
        return []

    # 1단계: 한국어 접속사, 영문 and, 쉼표로 토큰 분리
    # "69(4) 및 106"   → ["69(4)", "106"]
    # "20(3)과 (4)"    → ["20(3)", "(4)"]
    # "2, 8, 29, 37"   → ["2", "8", "29", "37"]
    # "40A, 40B, 40D"  → ["40A", "40B", "40D"]
    split_pattern = r"\s*(?:및|또는|과|and|,)\s*"
    tokens = re.split(split_pattern, range_str)

    result = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue

        # 2단계: 서브아이템 제거 - `(숫자+)`, `(한글자+)` 패턴 반복 제거
        # "106(4)(나)" → "106(4)" → "106"
        # "(4)"         → ""  (베이스 없는 토큰 — 버림)
        base = token
        # 후행 서브아이템 괄호를 반복 제거 (숫자 또는 한글 1~4자)
        sub_item_pattern = re.compile(r"\(\d+\)|\([가-힣]{1,4}\)")
        prev = None
        while prev != base:
            prev = base
            base = sub_item_pattern.sub("", base).strip()

        # 3단계: 정리 후 유효한 베이스 번호만 포함
        # 빈 문자열 또는 접속사 잔여물 제거
        base = base.strip()
        if base:
            result.append(base)

    return result
