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


# --- First-attempt visibility (Task A) -------------------------------------

class RetryScriptedBrowser:
    """A stub whose per-iteration retry count is scripted. reset() advances to the
    next iteration's value, mimicking BrowserTool zeroing-then-accumulating."""

    def __init__(self, retry_sequence: list[int]) -> None:
        self._seq = retry_sequence
        self._i = -1
        self._iteration_retries = 0

    async def reset(self) -> None:
        self._i += 1
        self._iteration_retries = self._seq[self._i]


async def test_first_attempt_counters_split_retried_from_clean():
    # 5 successful iterations: two needed retries (2 and 1), three were clean.
    seq = [0, 0, 2, 1, 0]
    rep = await run_reliability(ok_task, "stub_retry", n=5, browser=RetryScriptedBrowser(seq))
    assert rep.success == 5
    assert rep.first_attempt_success == 3          # the three zero-retry iterations
    assert rep.first_attempt_success_rate == 0.6
    assert rep.iterations_with_retry == 2          # the 2-retry and 1-retry iterations
    assert rep.total_retries == 3                  # 2 + 1


async def test_stub_without_counter_treated_as_first_attempt():
    # StubBrowser has no _iteration_retries; getattr guard -> 0 -> all first-attempt.
    rep = await run_reliability(ok_task, "stub_no_counter", n=4, browser=StubBrowser())
    assert rep.first_attempt_success == 4
    assert rep.iterations_with_retry == 0
    assert rep.total_retries == 0
