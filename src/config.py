"""
K-IFRS 크롤러 설정 상수

KASB(한국회계기준원) 웹사이트 크롤링에 필요한 URL, CSS 셀렉터,
타임아웃, 재시도 횟수, 기준서 번호 목록 등 전역 상수를 정의합니다.
크롤러 동작 파라미터를 한 곳에서 관리하여 수정이 용이하도록 합니다.

수집 대상:
    - K-IFRS 기업회계기준서 42개 (1001~1117)
    - K-IFRIC 기업회계기준해석서 19개 (2010~2123)
    - K-IFRS 개념체계 1개 (CF)
    - 특수문서 3개 (경영진설명서, 중요성판단, 적용의견서)

주의:
    - 4xxx는 K-GAAP(일반기업회계기준) 해석서로 K-IFRS 아님 → 제외
    - 특수문서는 논리 ID(MC, MP, AO)와 사이트 내부 ID가 다름 → SPECIAL_DOCUMENTS 참조
"""
from typing import Final

# 기본 URL
BASE_URL: Final[str] = "https://db.kasb.or.kr"
STANDARD_LIST_URL: Final[str] = "https://db.kasb.or.kr/standard"

# K-IFRS 기업회계기준서 번호 목록 (42개, 1001~1117)
KIFRS_STANDARDS: Final[list[str]] = [
    "1001", "1002", "1007", "1008", "1010", "1012", "1016", "1019", "1020", "1021",
    "1023", "1024", "1026", "1027", "1028", "1029", "1032", "1033", "1034", "1036",
    "1037", "1038", "1039", "1040", "1041", "1101", "1102", "1103", "1105",
    "1106", "1107", "1108", "1109", "1110", "1111", "1112", "1113", "1114", "1115",
    "1116", "1117",
]

# K-IFRIC 기업회계기준해석서 번호 목록 (19개)
# 4xxx(K-GAAP 해석서)는 포함하지 않음 — /s/4xxx는 undefined로 리다이렉트됨
KIFRIC_STANDARDS: Final[list[str]] = [
    "2010", "2025", "2029", "2032",
    "2101", "2102", "2105", "2106", "2107", "2110", "2112", "2114", "2116", "2117",
    "2119", "2120", "2121", "2122", "2123",
]

# K-IFRS 개념체계 (1개)
CONCEPTUAL_FRAMEWORK: Final[list[str]] = ["CF"]

# 특수문서 (번역서, 적용의견서 등)
# internal_id: 사이트에서 실제 사용하는 내부 식별자 (/s/{internal_id} URL)
SPECIAL_DOCUMENTS: Final[list[dict]] = [
    {"id": "MC", "internal_id": "1191", "title": "경영진설명서 작성을 위한 개념체계 번역서", "type": "번역서"},
    {"id": "MP", "internal_id": "1192", "title": "중요성에 대한 판단 번역서", "type": "번역서"},
    {"id": "AO", "internal_id": "10121", "title": "회계기준 적용의견서", "type": "적용의견서"},
]

# 타임아웃 설정 (밀리초)
PAGE_LOAD_TIMEOUT: Final[int] = 30_000
NETWORK_IDLE_TIMEOUT: Final[int] = 15_000

# 재시도 설정
MAX_RETRIES: Final[int] = 3
RETRY_DELAYS: Final[list[float]] = [5.0, 10.0, 20.0]

# 기준서 간 딜레이 (초)
INTER_STANDARD_DELAY: Final[float] = 1.5

# CSS 셀렉터 상수
SELECTORS = {
    # TOC 관련
    "toc_links": "aside a[href]",
    # 문단 관련
    "paragraph_items": "li[data-paranum]",
    "paragraph_inner": ".para-inner-para",
    # 교차참조 관련
    "std_finder": ".std-finder",
    "tooltip_content": ".tooltip-content",
    "mundan_finder": ".mundan-finder",
    # 질의회신 관련
    "qna_button": "button",
    "qna_links": "a[href^='/qnas/']",
}

# 표준 유형 매핑 (첫 자리 → 유형)
STANDARD_TYPE_MAP = {
    "1": "기업회계기준서",
    "2": "기업회계기준해석서",
    "CF": "개념체계",
    "MC": "번역서",
    "MP": "번역서",
    "AO": "적용의견서",
}
