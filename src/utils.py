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

# 특수 유니코드 괄호 넘버링 → 일반 괄호 변환 테이블
# ⑴~⒇ (U+2474~U+2487): Parenthesized Digit → (1)~(20)
# ㈎~㈛ (U+320E~U+321B): Parenthesized Hangul → (가)~(하)
_PARENS_DIGIT_MAP = {chr(0x2474 + i): f"({i + 1})" for i in range(20)}
_PARENS_HANGUL_MAP = {chr(0x320E + i): f"({'가나다라마바사아자차카타파하'[i]})" for i in range(14)}
_UNICODE_PARENS_TABLE = str.maketrans({**_PARENS_DIGIT_MAP, **_PARENS_HANGUL_MAP})


def normalize_unicode_parens(text: str) -> str:
    """⑴~⒇ → (1)~(20), ㈎~㈛ → (가)~(하) 변환"""
    return text.translate(_UNICODE_PARENS_TABLE)


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
    text = normalize_unicode_parens(text)  # ⑴~⒇, ㈎~㈛ → 일반 괄호 (개행 처리 전 변환)
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
            # 비테이블 텍스트: 항목 구분 개행만 보존, 나머지는 공백으로 변환
            # \n\n+ (pip↔pip DOM 아티팩트) → 공백
            t = "\n".join(blines)
            # 항목 패턴([각주, (숫자), ㈎ 등) 앞의 연속 개행을 단일 \n으로 보존
            t = re.sub(r"\n+(?=[(\[㈎-㈿])", "\n", t)
            # 그 외 \n\n+ → 공백 (DOM 아티팩트 제거)
            t = re.sub(r"\n{2,}", " ", t)
            # 단일 \n → 공백 (항목 패턴 앞 제외)
            t = re.sub(r"\n(?![(\[㈎-㈿])", " ", t)
            # 각주 마커 (한N), (주N) 앞의 개행 제거 — 문장 끝에 인라인으로 붙음
            t = re.sub(r"\n(?=\((?:한|주)\d+\))", "", t)
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
        "IFRS 17" → "17"
        "IAS 1" → "1"
    """
    # K-IFRS 형식: "제XXXX호"
    match = re.search(r"제(\d+)호", display_text)
    if match:
        return match.group(1)
    # 국제 기준서 형식: "IFRS N" 또는 "IAS N"
    match = re.search(r"(?:IFRS|IAS)\s+(\d+)", display_text)
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
        "3(1)"             → ["3"]
        "106(4)(나)"       → ["106"]
        "6.5.11(4)"        → ["6.5.11"]
        "20(3)과 (4)"      → ["20"]
        "69(4) 및 106"     → ["69", "106"]
        "15~35"            → ["15~35"]
        "15"               → ["15"]
        "40A(1)"           → ["40A"]
        "BC13H⑴․⑶․⑷"     → ["BC13H"]   (U+2024 가운뎃점 분리자 처리)
        "93)"              → ["93"]       (후행 닫는 괄호 제거)
        "7과 문단 96"      → ["7", "96"]  (분리 후 토큰 내 '문단' 접두사 제거)
    """
    if not range_str:
        return []

    # 1단계: 한국어 접속사, 영문 and, 쉼표, U+2024(ONE DOT LEADER ․)로 토큰 분리
    # "69(4) 및 106"      → ["69(4)", "106"]
    # "20(3)과 (4)"       → ["20(3)", "(4)"]
    # "2, 8, 29, 37"      → ["2", "8", "29", "37"]
    # "40A, 40B, 40D"     → ["40A", "40B", "40D"]
    # "BC13H⑴․⑶․⑷"      → ["BC13H⑴", "⑶", "⑷"]  (U+2024 분리)
    split_pattern = r"\s*(?:및|또는|과|와|and|,|\u2024)\s*"
    tokens = re.split(split_pattern, range_str)

    result = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue

        # 1.5단계: 토큰 앞의 "문단" 접두사 제거
        # 접속사로 분리된 후 남은 "문단 96" → "96"
        # (extract_paragraph_range가 전체 앞의 "문단"을 제거하지만,
        #  "A과 문단 B" 형태에서 분리 후 "문단 B"가 남는 경우 처리)
        token = re.sub(r"^문단\s*", "", token)

        # 2단계: 서브아이템 제거 - `(숫자+)`, `(한글자+)` 패턴 반복 제거
        # "106(4)(나)" → "106(4)" → "106"
        # "(4)"         → ""  (베이스 없는 토큰 — 버림)
        base = token
        # 후행 서브아이템 괄호를 반복 제거 (숫자 또는 한글 1~4자)
        # ⑴~⒇ (U+2474~U+2487): Parenthesized Digit/Number 단일 유니코드 문자
        # ㈀~㈻ (U+3200~U+321B): Parenthesized Hangul 단일 유니코드 문자
        sub_item_pattern = re.compile(r"\(\d+\)|\([가-힣]{1,4}\)|[\u2474-\u2487]|[\u3200-\u321b]")
        prev = None
        while prev != base:
            prev = base
            base = sub_item_pattern.sub("", base).strip()

        # 3단계: 후행 닫는 괄호 제거
        # "93)" → "93"  (문장 닫는 괄호가 span 범위에 포함된 사이트 데이터 문제 방어)
        base = re.sub(r"\)+$", "", base).strip()

        # 4단계: 정리 후 유효한 베이스 번호만 포함
        # 빈 문자열 또는 접속사 잔여물 제거
        if base:
            result.append(base)

    return result
