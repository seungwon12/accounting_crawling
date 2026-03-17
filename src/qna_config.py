"""
QnA 크롤러 설정 상수

KASB QnA REST API 호출에 필요한 URL, 타입 코드 매핑, 딜레이,
재시도 횟수, 섹션 분류 키워드 등 전역 상수를 정의합니다.

API 엔드포인트:
    - 목록: GET /api/qnas/v2?types=all&page={n}&rows=100
    - 상세: GET /api/qnas/v2/{docNumber}

수집 대상 (총 2,243건):
    - 11: K-IFRS 회계기준원 (116건)
    - 12: 일반기업회계기준 회계기준원 (557건)
    - 13: K-IFRS IFRS 해석위원회 논의결과 (174건)
    - 14: 일반기업회계기준 신속처리질의 (17건)
    - 15: K-IFRS 신속처리질의 (572건)
    - 24: 일반기업회계기준 금융감독원 (759건)
    - 25: K-IFRS 금융감독원 (48건)
"""
from typing import Final

# API 기본 URL (기준서 크롤러와 동일한 도메인)
QNA_API_BASE: Final[str] = "https://db.kasb.or.kr/api/qnas/v2"

# 목록 API 파라미터 기본값
QNA_LIST_ROWS: Final[int] = 100   # 한 페이지당 최대 100건

# QnA 타입 코드 → 이름 매핑
# | 코드 | 분류            | 카테고리                    | 건수 |
# |------|-----------------|----------------------------|------|
# |  11  | K-IFRS          | 회계기준원                  |  116 |
# |  13  | K-IFRS          | IFRS 해석위원회 논의결과     |  174 |
# |  15  | K-IFRS          | 신속처리질의                |  572 |
# |  25  | K-IFRS          | 금융감독원                  |   48 |
# |  12  | 일반기업회계기준 | 회계기준원                  |  557 |
# |  14  | 일반기업회계기준 | 신속처리질의                |   17 |
# |  24  | 일반기업회계기준 | 금융감독원                  |  759 |
QNA_TYPE_MAP: Final[dict[int, str]] = {
    11: "K-IFRS 회계기준원",
    13: "K-IFRS IFRS 해석위원회 논의결과",
    15: "K-IFRS 신속처리질의",
    25: "K-IFRS 금융감독원",
    12: "일반기업회계기준 회계기준원",
    14: "일반기업회계기준 신속처리질의",
    24: "일반기업회계기준 금융감독원",
}

# API 요청 딜레이 (서버 부하 방지)
QNA_REQUEST_DELAY_MIN: Final[float] = 0.3   # 초
QNA_REQUEST_DELAY_MAX: Final[float] = 0.5   # 초

# 재시도 설정
QNA_MAX_RETRIES: Final[int] = 3
QNA_RETRY_DELAYS: Final[list[float]] = [2.0, 5.0, 10.0]

# HTTP 타임아웃 (초)
QNA_TIMEOUT: Final[float] = 30.0

# "질의" 섹션 heading 키워드 (이 텍스트가 포함된 섹션 → question 필드)
QUESTION_HEADINGS: Final[list[str]] = ["배경 및 질의", "질의", "사실관계", "배경"]

# "회신" 섹션 heading 키워드 (이 텍스트가 포함된 섹션 → answer 필드)
ANSWER_HEADINGS: Final[list[str]] = ["회신"]
