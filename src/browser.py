"""
Playwright 브라우저 관리 모듈

Chromium 브라우저 생명주기 관리와 페이지 이동 재시도 로직을 제공합니다.

주요 클래스/함수:
    - BrowserManager: async with 문으로 브라우저를 시작/종료하는 컨텍스트 매니저
    - navigate_with_retry: 지수 백오프를 적용한 페이지 이동 헬퍼
    - get_browser: BrowserManager를 asynccontextmanager로 감싼 팩토리 함수

사용 예시::

    async with BrowserManager(headless=True) as browser:
        page = await browser.new_page()
        success = await navigate_with_retry(page, "https://db.kasb.or.kr/s/1001")
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from src.config import PAGE_LOAD_TIMEOUT, NETWORK_IDLE_TIMEOUT, MAX_RETRIES, RETRY_DELAYS
from src.utils import logger


class BrowserManager:
    """
    Playwright 브라우저 생명주기를 관리하는 비동기 컨텍스트 매니저.

    async with 문으로 브라우저 시작/종료를 자동 처리합니다.
    Chromium 브라우저를 헤드리스(기본) 또는 GUI 모드로 실행하며,
    봇 차단 방지를 위한 User-Agent와 AutomationControlled 비활성화를 적용합니다.

    사용 예시::

        async with BrowserManager(headless=True) as browser:
            page = await browser.new_page()
            await page.goto("https://db.kasb.or.kr/s/1001")
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "BrowserManager":
        """Playwright와 Chromium 브라우저를 시작합니다."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._context.set_default_timeout(PAGE_LOAD_TIMEOUT)
        logger.info("브라우저 시작됨 (headless=%s)", self.headless)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """컨텍스트, 브라우저, Playwright를 역순으로 종료합니다."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("브라우저 종료됨")

    async def new_page(self) -> Page:
        """새 페이지를 생성합니다."""
        assert self._context is not None, "브라우저가 초기화되지 않았습니다"
        return await self._context.new_page()


async def navigate_with_retry(
    page: Page,
    url: str,
    max_retries: int = MAX_RETRIES,
    wait_until: str = "networkidle",
) -> bool:
    """
    지수 백오프를 적용한 페이지 이동 헬퍼.

    Returns:
        성공 여부
    """
    for attempt in range(max_retries):
        try:
            logger.debug("페이지 이동 시도 %d/%d: %s", attempt + 1, max_retries, url)
            await page.goto(
                url,
                wait_until=wait_until,
                timeout=PAGE_LOAD_TIMEOUT,
            )
            # networkidle 추가 대기
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
            return True
        except Exception as e:
            logger.warning("페이지 이동 실패 (시도 %d): %s - %s", attempt + 1, url, e)
            if attempt < max_retries - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.info("%.0f초 후 재시도...", delay)
                await asyncio.sleep(delay)
            else:
                logger.error("최대 재시도 횟수 초과: %s", url)
                return False
    return False


@asynccontextmanager
async def get_browser(headless: bool = True) -> AsyncGenerator[BrowserManager, None]:
    """브라우저 매니저 컨텍스트 팩토리"""
    manager = BrowserManager(headless=headless)
    async with manager as m:
        yield m
