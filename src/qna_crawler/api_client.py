"""
KASB QnA REST API 클라이언트

httpx.AsyncClient 기반 비동기 클라이언트로, 목록/상세 API 호출과
지수 백오프 재시도, 랜덤 딜레이 레이트 리밋을 제공합니다.

주의사항:
    - KASB 서버는 자체 서명 인증서를 사용하므로 verify=False 필수
    - 봇 차단 방지를 위해 브라우저와 동일한 User-Agent/헤더 설정

사용 예시::

    async with QnaApiClient() as client:
        # 목록 API: 0-based 페이지 번호
        data = await client.fetch_qna_list(page=0)
        # 상세 API: docNumber 직접 지정
        detail = await client.fetch_qna_detail("2020-I-KQA009")
"""
import asyncio
import random
from typing import Any, Optional

import httpx

from src.qna_config import (
    QNA_API_BASE,
    QNA_LIST_ROWS,
    QNA_MAX_RETRIES,
    QNA_REQUEST_DELAY_MAX,
    QNA_REQUEST_DELAY_MIN,
    QNA_RETRY_DELAYS,
    QNA_TIMEOUT,
)
from src.utils import logger

# 브라우저처럼 보이는 헤더 (봇 차단 방지)
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://db.kasb.or.kr/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


class QnaApiClient:
    """
    KASB QnA REST API 클라이언트.

    사용법:
        async with QnaApiClient() as client:
            items = await client.fetch_qna_list(page=0)
            detail = await client.fetch_qna_detail("2020-I-KQA009")
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "QnaApiClient":
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=QNA_TIMEOUT,
            follow_redirects=True,
            verify=False,   # KASB 서버는 자체 서명 인증서 사용
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, url: str, params: Optional[dict] = None) -> dict:
        """
        GET 요청을 수행합니다. 실패 시 지수 백오프로 재시도.
        """
        assert self._client is not None, "컨텍스트 매니저 안에서 사용하세요"

        for attempt in range(QNA_MAX_RETRIES):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 404:
                    # 404는 재시도하지 않음 (존재하지 않는 리소스)
                    logger.warning("404 Not Found: %s", url)
                    return {}
                logger.warning(
                    "HTTP 오류 (시도 %d/%d): %s %s",
                    attempt + 1, QNA_MAX_RETRIES, status, url
                )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning(
                    "네트워크 오류 (시도 %d/%d): %s - %s",
                    attempt + 1, QNA_MAX_RETRIES, url, e
                )

            # 마지막 시도가 아니면 딜레이 후 재시도
            if attempt < QNA_MAX_RETRIES - 1:
                delay = QNA_RETRY_DELAYS[min(attempt, len(QNA_RETRY_DELAYS) - 1)]
                logger.info("%.1f초 후 재시도...", delay)
                await asyncio.sleep(delay)

        logger.error("최대 재시도 초과: %s", url)
        return {}

    async def _rate_limit_delay(self) -> None:
        """요청 간 랜덤 딜레이로 서버 부하 방지"""
        delay = random.uniform(QNA_REQUEST_DELAY_MIN, QNA_REQUEST_DELAY_MAX)
        await asyncio.sleep(delay)

    async def fetch_qna_list(self, page: int, rows: int = QNA_LIST_ROWS) -> dict:
        """
        QnA 목록 API를 호출합니다.

        GET /api/qnas/v2?types=all&page={page}&rows={rows}

        실제 응답:
        {
            "status": 200,
            "facilityQnas": [...],           ← 항목 목록
            "facilityQnaCountData": {"11": 116, ...}  ← 타입별 건수
        }

        반환: {"items": [...], "total_count": 2243}
        """
        params = {"types": "all", "page": page, "rows": rows}
        logger.debug("목록 API 호출: page=%d, rows=%d", page, rows)
        raw = await self._request(QNA_API_BASE, params=params)
        await self._rate_limit_delay()

        items = raw.get("facilityQnas") or []
        count_data = raw.get("facilityQnaCountData") or {}
        total = sum(count_data.values()) if count_data else 0
        return {"items": items, "total_count": total}

    async def fetch_qna_detail(self, doc_number: str) -> dict:
        """
        QnA 상세 API를 호출합니다.

        GET /api/qnas/v2/{doc_number}

        실제 응답:
        {
            "status": 200,
            "facilityQna": {
                "id": ..., "docNumber": "2020-I-KQA009",
                "contentHtml": "<section>...",
                "bookmarkStdParagraphs": {"1001": ["4", "38"]},
                ...
            }
        }

        반환: facilityQna dict (언래핑됨)
        """
        url = f"{QNA_API_BASE}/{doc_number}"
        logger.debug("상세 API 호출: %s", doc_number)
        raw = await self._request(url)
        await self._rate_limit_delay()
        return raw.get("facilityQna") or {}
