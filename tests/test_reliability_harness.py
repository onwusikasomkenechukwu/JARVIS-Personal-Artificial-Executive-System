"""Acceptance test for milestone 0's harness *mechanics*.

These run a 10x mini-version with a stub browser to prove the harness classifies
outcomes correctly and reports clean structured results with zero harness-class
errors on success. The real 100x browser run is executed manually by the user
(python -m jarvis.harness.reliability) — its job is the brittleness number, not CI.
"""
from jarvis.harness.reliability import run_reliability
from jarvis.tools.browser import ToolResult


class StubBrowser:
    async def reset(self) -> None:
        pass


async def ok_task(_b) -> ToolResult:
    return ToolResult(ok=True, value="done")


async def browser_fail_task(_b) -> ToolResult:
    return ToolResult(ok=False, error="timeout", error_class="browser")


async def raising_task(_b) -> ToolResult:
    raise RuntimeError("event loop misuse")  # an asyncio bug → harness-class


async def test_all_success_clean_report():
    rep = await run_reliability(ok_task, "stub_ok", n=10, browser=StubBrowser())
    assert rep.total == 10
    assert rep.success == 10
    assert rep.failure == 0
    assert rep.failure_rate == 0.0
    assert rep.harness_error_count == 0  # the critical requirement


async def test_browser_failures_classified_as_browser():
    rep = await run_reliability(browser_fail_task, "stub_browser_fail", n=10, browser=StubBrowser())
    assert rep.failure == 10
    assert rep.error_class_histogram.get("browser") == 10
    assert rep.harness_error_count == 0


async def test_task_exception_is_harness_class_not_browser():
    rep = await run_reliability(raising_task, "stub_raise", n=5, browser=StubBrowser())
    assert rep.failure == 5
    assert rep.harness_error_count == 5
    assert rep.error_class_histogram.get("browser", 0) == 0
    assert rep.harness_error_rate == 1.0
