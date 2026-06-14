"""Async Playwright browser executor.

Four primitives: navigate / read / fill / click. Each wraps Playwright with an
explicit timeout and bounded retry, and returns a *structured* result that
distinguishes a genuine browser failure from a harness/programming bug:

    {ok, value, attempts, error, error_class}  where error_class ∈ {browser, harness}

That distinction is load-bearing for the reliability measurement: an asyncio
plumbing bug (unawaited coroutine, loop torn down on exception) must never be
counted as a browser data point. Playwright errors are `browser`; anything else is
`harness`.

Playwright is imported lazily so the rest of the package (and the harness unit
tests) can be imported without the browser binaries installed.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import BaseModel

from ..config import get_logger, settings

log = get_logger("jarvis.browser")

ErrorClass = Literal["browser", "harness"]


class ToolResult(BaseModel):
    ok: bool
    value: Any = None
    attempts: int = 0
    error: Optional[str] = None
    error_class: Optional[ErrorClass] = None


class BrowserTool:
    """One Chromium process. Use a fresh context per reliability iteration via
    reset() so runs don't share cookies/state."""

    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        retries: int | None = None,
    ) -> None:
        self.headless = settings.browser_headless if headless is None else headless
        self.timeout_ms = timeout_ms or settings.browser_timeout_ms
        self.retries = retries or settings.browser_retries
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        # Retry events accumulated within the current iteration (extra attempts
        # beyond the first, summed across every primitive). Zeroed by reset().
        self._iteration_retries = 0

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        await self._new_context()

    async def _new_context(self) -> None:
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    async def reset(self) -> None:
        """Drop the current context and open a clean one (fresh state per run)."""
        self._iteration_retries = 0
        if self._context is not None:
            await self._context.close()
        await self._new_context()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()

    async def _guard(self, op: str, fn: Callable[[], Awaitable[Any]]) -> ToolResult:
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout

        last_err: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            if attempt > 1:
                # An attempt beyond the first is a retry event for this iteration.
                self._iteration_retries += 1
            try:
                value = await fn()
                return ToolResult(ok=True, value=value, attempts=attempt)
            except (PWTimeout, PWError) as e:  # a real browser-side failure → retry
                last_err = f"{type(e).__name__}: {e}".strip()
                log.warning("browser_retry", op=op, attempt=attempt, error=last_err)
                await asyncio.sleep(0.5 * attempt)  # linear backoff
            except Exception as e:  # not a browser failure — a harness/programming bug
                log.error("harness_error", op=op, error=f"{type(e).__name__}: {e}")
                return ToolResult(
                    ok=False,
                    attempts=attempt,
                    error=f"{type(e).__name__}: {e}",
                    error_class="harness",
                )
        return ToolResult(ok=False, attempts=self.retries, error=last_err, error_class="browser")

    async def navigate(self, url: str) -> ToolResult:
        async def op() -> Any:
            resp = await self._page.goto(url, wait_until="domcontentloaded")
            return resp.status if resp is not None else None

        return await self._guard("navigate", op)

    async def read(self, selector: str | None = None) -> ToolResult:
        async def op() -> str:
            target = selector if selector is not None else "body"
            return await self._page.inner_text(target)

        return await self._guard("read", op)

    async def fill(self, selector: str, value: str) -> ToolResult:
        return await self._guard("fill", lambda: self._page.fill(selector, value))

    async def click(self, selector: str) -> ToolResult:
        return await self._guard("click", lambda: self._page.click(selector))
