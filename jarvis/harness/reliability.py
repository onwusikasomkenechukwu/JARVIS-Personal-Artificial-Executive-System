"""MILESTONE 0 — the browser reliability gate.

Runs one task definition N times and reports the failure rate, broken down by
error_class. The whole point is to produce a *trustworthy* brittleness number:

  * `browser`-class failures are the real signal (selectors break, pages change).
  * `harness`-class failures are bugs in *this* code (asyncio misuse, setup/teardown)
    and are NOT browser data points. If the harness's own harness-class rate is above
    ~1%, the harness is buggy and its browser numbers are not yet trustworthy.

Two stages (run manually via the CLI at the bottom):
  Stage A — books.toscrape.com : a scrape-friendly site; expect ~99%+. Validates the
            harness itself. If you can't clear ~99% here, fix the harness, not the web.
  Stage B — en.wikipedia.org   : a real, complex, read-only DOM. The Stage B
            browser-failure rate is the number that gates the project. Wikipedia is
            cooperative, so treat it as a floor on difficulty, not a representative one.

Run:
    python -m jarvis.harness.reliability --target books --n 100
    python -m jarvis.harness.reliability --target wikipedia --n 100 --headed
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..config import configure_logging, get_logger
from ..tools.browser import BrowserTool, ToolResult

log = get_logger("jarvis.harness")

# A task takes a browser and returns a final ToolResult (ok + error_class on failure).
Task = Callable[[BrowserTool], Awaitable[ToolResult]]


@dataclass
class FailureDetail:
    iteration: int
    error_class: str
    error: Optional[str]


@dataclass
class ReliabilityReport:
    task_name: str
    total: int
    success: int
    failure: int
    error_class_histogram: dict[str, int]
    failures: list[FailureDetail] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return self.failure / self.total if self.total else 0.0

    @property
    def harness_error_count(self) -> int:
        return self.error_class_histogram.get("harness", 0)

    @property
    def harness_error_rate(self) -> float:
        return self.harness_error_count / self.total if self.total else 0.0

    @property
    def browser_failure_count(self) -> int:
        return self.error_class_histogram.get("browser", 0)

    @property
    def browser_failure_rate(self) -> float:
        return self.browser_failure_count / self.total if self.total else 0.0

    def render(self) -> str:
        lines = [
            f"Reliability report — task '{self.task_name}'",
            f"  runs:              {self.total}",
            f"  success:           {self.success}",
            f"  failure:           {self.failure}  ({self.failure_rate:.1%})",
            f"  browser-class:     {self.browser_failure_count}  ({self.browser_failure_rate:.1%})  <- the gating signal",
            f"  harness-class:     {self.harness_error_count}  ({self.harness_error_rate:.1%})  <- must be ~0 to trust the above",
        ]
        if self.harness_error_rate > 0.01:
            lines.append("  !! harness-class rate > 1% — the harness is buggy; browser numbers are NOT trustworthy yet.")
        if self.failures:
            lines.append("  failures (first 10):")
            for f in self.failures[:10]:
                lines.append(f"    #{f.iteration} [{f.error_class}] {f.error}")
        return "\n".join(lines)


async def run_reliability(
    task: Task,
    task_name: str,
    n: int = 100,
    browser: Optional[BrowserTool] = None,
    reset_between: bool = True,
) -> ReliabilityReport:
    """Run `task` n times. Pass `browser` to inject a stub (tests); otherwise a real
    BrowserTool is created and torn down here."""
    owns_browser = browser is None
    if owns_browser:
        browser = BrowserTool()
        await browser.start()

    hist: Counter[str] = Counter()
    failures: list[FailureDetail] = []
    success = 0
    try:
        for i in range(1, n + 1):
            if reset_between and hasattr(browser, "reset"):
                try:
                    await browser.reset()
                except Exception as e:  # a fresh-context failure is a harness problem
                    hist["harness"] += 1
                    failures.append(FailureDetail(i, "harness", f"reset failed: {type(e).__name__}: {e}"))
                    continue
            try:
                result = await task(browser)
            except Exception as e:
                # An exception escaping the task itself is harness-level, never a
                # browser reliability data point.
                hist["harness"] += 1
                failures.append(FailureDetail(i, "harness", f"{type(e).__name__}: {e}"))
                continue

            if result.ok:
                success += 1
            else:
                ec = result.error_class or "browser"
                hist[ec] += 1
                failures.append(FailureDetail(i, ec, result.error))
    finally:
        if owns_browser:
            await browser.close()

    return ReliabilityReport(task_name, n, success, n - success, dict(hist), failures)


# --- Task definitions -------------------------------------------------------

async def task_books(b: BrowserTool) -> ToolResult:
    """Stage A: scrape-friendly. Read a title, open the detail page, read the price."""
    r = await b.navigate("https://books.toscrape.com/")
    if not r.ok:
        return r
    r = await b.read("article.product_pod h3 a")
    if not r.ok:
        return r
    title = r.value
    r = await b.click("article.product_pod h3 a")
    if not r.ok:
        return r
    r = await b.read("p.price_color")
    if not r.ok:
        return r
    return ToolResult(ok=True, value={"title": title, "price": r.value})


async def task_wikipedia(b: BrowserTool) -> ToolResult:
    """Stage B (gating): search a fixed query, open the article, read the first paragraph."""
    r = await b.navigate("https://en.wikipedia.org/")
    if not r.ok:
        return r
    r = await b.fill("#searchInput", "photoplethysmography")
    if not r.ok:
        return r
    r = await b.click("#searchform button")
    if not r.ok:
        return r
    r = await b.read("#mw-content-text .mw-parser-output > p:not(.mw-empty-elt)")
    if not r.ok:
        return r
    text = (r.value or "").strip()
    return ToolResult(ok=bool(text), value=text[:200], error=None if text else "empty paragraph", error_class=None if text else "browser")


TASKS: dict[str, Task] = {"books": task_books, "wikipedia": task_wikipedia}


async def _amain(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis.harness.reliability")
    parser.add_argument("--target", choices=sorted(TASKS), default="books")
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)

    configure_logging()
    browser = BrowserTool(headless=not args.headed)
    await browser.start()
    try:
        report = await run_reliability(TASKS[args.target], args.target, n=args.n, browser=browser)
    finally:
        await browser.close()
    print(report.render())


if __name__ == "__main__":
    asyncio.run(_amain())
