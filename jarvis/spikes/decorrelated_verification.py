"""Decorrelated-verification spike.

Question this experiment answers:

    Can an auditor confirm a side-effecting action actually happened, by checking
    ground-truth state through a channel that did NOT consume the same untrusted
    input that drove the action — and what does that cost?

Action under test: an *untrusted-derived memory write*. A browser reads a fact off a
real web page (untrusted), and the fact is written to the `facts` store. The verifier
then confirms the write **by querying the fact store only** — never re-reading the
page. The driving input arrives through the browser; verification arrives through the
DB. Those are different channels, which is the decorrelation being tested.

Discipline (the whole point): the verifier compares DB state against the *typed
request* (what the system said it would write), NOT against the world (a fresh page
read). It checks "did the system do what the request said," which is verifiable
without the untrusted source. It does NOT check "is this fact true," which would
require the page and is deliberately left on the untrusted side.

Structural proof of decorrelation: `verify_memory_write` takes a repository and a
typed request. It is never handed a browser or a page. It *cannot* read the untrusted
source, so its contribution to `untrusted_read_count` is zero by construction — and
the spike measures it anyway rather than asserting it.

See docs/verification-spike-findings.md for the findings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from ..memory.store import FactType, check_write_allowed, compute_expiry, is_stale
from ..requests import ActionRequest, Provenance


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Fact repository (the decorrelated channel) ----------------------------

class FactRepo(Protocol):
    """The verification channel. Both the write action and the verifier reach the
    fact store only through this — there is no page handle anywhere in it."""

    async def insert(self, fact: dict) -> int: ...
    async def get(self, fact_id: int) -> Optional[dict]: ...
    async def find_logical_duplicates(self, content: str, source: str, trust_label: str) -> list[dict]: ...


class InMemoryFactRepository:
    """Fixture impl: an in-memory facts table. Used by tests and by the spike runner
    when Postgres is not available. Durability is the only thing it does not model;
    the read-count and decorrelation properties are identical to the asyncpg path."""

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._next_id = 1
        self.get_calls = 0  # so a test can prove the verifier used the DB channel

    async def insert(self, fact: dict) -> int:
        row = dict(fact)
        row["id"] = self._next_id
        self._next_id += 1
        self._rows.append(row)
        return row["id"]

    async def get(self, fact_id: int) -> Optional[dict]:
        self.get_calls += 1
        for row in self._rows:
            if row["id"] == fact_id:
                return dict(row)
        return None

    async def find_logical_duplicates(self, content: str, source: str, trust_label: str) -> list[dict]:
        return [
            dict(r)
            for r in self._rows
            if r["content"] == content and r["source"] == source and r["trust_label"] == trust_label
        ]


class AsyncpgFactRepository:
    """Production / real-run impl over the existing `facts` table. The user runs the
    spike against real Postgres through this; the queries touch only the DB."""

    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, fact: dict) -> int:
        row = await self._conn.fetchrow(
            """
            INSERT INTO facts (content, source, trust_label, fact_type, created_at, expires_at, stale, reviewed)
            VALUES ($1, $2, $3, $4, $5, $6, false, $7)
            RETURNING id
            """,
            fact["content"], fact["source"], fact["trust_label"], fact["fact_type"],
            fact["created_at"], fact["expires_at"], fact["reviewed"],
        )
        return row["id"]

    async def get(self, fact_id: int) -> Optional[dict]:
        row = await self._conn.fetchrow("SELECT * FROM facts WHERE id = $1", fact_id)
        return dict(row) if row is not None else None

    async def find_logical_duplicates(self, content: str, source: str, trust_label: str) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT * FROM facts WHERE content = $1 AND source = $2 AND trust_label = $3",
            content, source, trust_label,
        )
        return [dict(r) for r in rows]


# --- The write action (untrusted-derived memory write) ---------------------

async def write_untrusted_fact(
    repo: FactRepo,
    request: ActionRequest,
    fact_type: FactType = FactType.VOLATILE,
    reviewed: bool = False,
    now: Optional[datetime] = None,
) -> int:
    """Write the fact carried by `request.args['content']` to the store, tagged with
    the request's provenance. Exercises the untrusted-write gate: an UNTRUSTED_DERIVED
    write without review is rejected. Returns the new fact id."""
    check_write_allowed(request.provenance, reviewed)  # the provenance gate
    created = now or _utcnow()
    fact = {
        "content": request.args["content"],
        "source": request.trigger_source,
        "trust_label": request.provenance.value,
        "fact_type": fact_type.value,
        "created_at": created,
        "expires_at": compute_expiry(fact_type, created),
        "reviewed": reviewed,
        "stale": False,
    }
    return await repo.insert(fact)


# --- The verifier (the actual spike) ---------------------------------------

@dataclass
class VerificationResult:
    exists: bool
    provenance_matches: bool
    source_matches: bool
    content_matches: bool
    duplicate_count: int

    @property
    def has_duplicates(self) -> bool:
        return self.duplicate_count > 1

    @property
    def verified(self) -> bool:
        return (
            self.exists
            and self.provenance_matches
            and self.source_matches
            and self.content_matches
            and not self.has_duplicates
        )


async def verify_memory_write(repo: FactRepo, request: ActionRequest, fact_id: int) -> VerificationResult:
    """Confirm the write happened by querying the store ONLY. Compares against the
    typed `request`, never against a fresh page read. Takes no browser/page: it is
    structurally incapable of re-consuming the untrusted source."""
    row = await repo.get(fact_id)
    if row is None:
        return VerificationResult(False, False, False, False, 0)

    provenance_matches = row["trust_label"] == request.provenance.value
    source_matches = row["source"] == request.trigger_source
    content_matches = row["content"] == request.args["content"]

    dups = await repo.find_logical_duplicates(row["content"], row["source"], row["trust_label"])
    return VerificationResult(
        exists=True,
        provenance_matches=provenance_matches,
        source_matches=source_matches,
        content_matches=content_matches,
        duplicate_count=len(dups),
    )


# --- End-to-end runner (real browser read -> write -> verify) --------------

@dataclass
class SpikeReport:
    url: str
    fact_id: Optional[int]
    executor_untrusted_reads: int   # page reads the action consumed (incl. retries)
    verifier_untrusted_reads: int   # MUST be 0
    verification: Optional[VerificationResult]
    note: str = ""
    extra: dict = field(default_factory=dict)

    def render(self) -> str:
        v = self.verification
        lines = [
            "Decorrelated-verification spike",
            f"  url:                      {self.url}",
            f"  fact_id:                  {self.fact_id}",
            f"  executor untrusted reads: {self.executor_untrusted_reads}",
            f"  verifier untrusted reads: {self.verifier_untrusted_reads}   (must be 0)",
        ]
        if v is not None:
            lines += [
                f"  exists:                   {v.exists}",
                f"  provenance matches:       {v.provenance_matches}",
                f"  source matches:           {v.source_matches}",
                f"  content matches request:  {v.content_matches}",
                f"  duplicate rows:           {v.duplicate_count}",
                f"  VERIFIED:                 {v.verified}",
            ]
        if self.note:
            lines.append(f"  note: {self.note}")
        return "\n".join(lines)


async def run_spike(browser, repo: FactRepo, url: str, selector: str, reviewed: bool = True) -> SpikeReport:
    """Read an untrusted fact off `url`, write it, then verify the write through the
    repo only. Measures the executor's and the verifier's untrusted-read counts."""
    # --- ACTION: executor consumes untrusted input through the browser ---
    reads_before_action = browser._read_attempts
    nav = await browser.navigate(url)
    if not nav.ok:
        return SpikeReport(url, None, browser._read_attempts - reads_before_action, 0, None, note=f"navigate failed: {nav.error}")
    rd = await browser.read(selector)
    if not rd.ok:
        return SpikeReport(url, None, browser._read_attempts - reads_before_action, 0, None, note=f"read failed: {rd.error}")
    content = (rd.value or "").strip()
    executor_reads = browser._read_attempts - reads_before_action

    request = ActionRequest(
        action="write_memory",
        args={"content": content},
        provenance=Provenance.UNTRUSTED_DERIVED,
        trigger_source=url,
        level=0,
    )
    fact_id = await write_untrusted_fact(repo, request, reviewed=reviewed)

    # --- VERIFY: through the DB channel; measure that it reads the page zero times ---
    reads_before_verify = browser._read_attempts
    result = await verify_memory_write(repo, request, fact_id)
    verifier_reads = browser._read_attempts - reads_before_verify

    return SpikeReport(url, fact_id, executor_reads, verifier_reads, result)


async def _amain(argv: list[str] | None = None) -> None:
    import argparse

    from ..config import configure_logging
    from ..tools.browser import BrowserTool

    parser = argparse.ArgumentParser(prog="jarvis.spikes.decorrelated_verification")
    parser.add_argument("--url", default="https://en.wikipedia.org/wiki/Photoplethysmography")
    parser.add_argument("--selector", default="#mw-content-text .mw-parser-output > p:not(.mw-empty-elt)")
    parser.add_argument("--postgres", action="store_true", help="use the real facts table instead of the in-memory fixture")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()
    browser = BrowserTool(headless=not args.headed)
    await browser.start()

    conn = None
    try:
        if args.postgres:
            from ..memory import db
            conn = await db.connect()
            await db.init_schema(conn)
            repo: FactRepo = AsyncpgFactRepository(conn)
        else:
            repo = InMemoryFactRepository()

        report = await run_spike(browser, repo, args.url, args.selector)
        print(report.render())
    finally:
        await browser.close()
        if conn is not None:
            await conn.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_amain())
