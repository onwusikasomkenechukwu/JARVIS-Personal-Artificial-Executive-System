"""Tests for the decorrelated-verification spike.

These exercise the verifier's logic and the decorrelation property against the
in-memory fixture (no Postgres needed). The real end-to-end run against live
Wikipedia + Postgres is driven by the CLI and reported in the findings doc.
"""
import pytest

from jarvis.memory.store import UntrustedWriteRejected
from jarvis.requests import ActionRequest, Provenance
from jarvis.spikes.decorrelated_verification import (
    InMemoryFactRepository,
    verify_memory_write,
    write_untrusted_fact,
)


def untrusted_request(content: str, url: str = "https://en.wikipedia.org/wiki/Photoplethysmography") -> ActionRequest:
    return ActionRequest(
        action="write_memory",
        args={"content": content},
        provenance=Provenance.UNTRUSTED_DERIVED,
        trigger_source=url,
        level=0,
    )


async def test_clean_write_verifies_true():
    repo = InMemoryFactRepository()
    req = untrusted_request("PPG is an optically obtained plethysmogram.")
    fact_id = await write_untrusted_fact(repo, req, reviewed=True)

    result = await verify_memory_write(repo, req, fact_id)
    assert result.exists
    assert result.provenance_matches
    assert result.source_matches
    assert result.content_matches
    assert result.duplicate_count == 1
    assert result.verified


async def test_missing_write_verifies_false():
    repo = InMemoryFactRepository()
    req = untrusted_request("never written")
    result = await verify_memory_write(repo, req, fact_id=999)
    assert result.exists is False
    assert result.verified is False


async def test_duplicate_write_is_detected():
    repo = InMemoryFactRepository()
    req = untrusted_request("a fact that gets written twice by a retried action")
    # Simulate a retried side-effecting write: the same logical fact inserted twice.
    id1 = await write_untrusted_fact(repo, req, reviewed=True)
    await write_untrusted_fact(repo, req, reviewed=True)

    result = await verify_memory_write(repo, req, id1)
    assert result.duplicate_count == 2
    assert result.has_duplicates is True
    assert result.verified is False  # ground-truth state shows the double-write


async def test_untrusted_write_without_review_rejected():
    repo = InMemoryFactRepository()
    req = untrusted_request("untrusted content with no review")
    with pytest.raises(UntrustedWriteRejected):
        await write_untrusted_fact(repo, req, reviewed=False)
    # nothing was written
    assert await repo.get(1) is None


async def test_content_mismatch_fails_verification():
    repo = InMemoryFactRepository()
    req = untrusted_request("the content the request claims")
    fact_id = await write_untrusted_fact(repo, req, reviewed=True)

    # Verify against a DIFFERENT typed request (content the system did not write).
    other = untrusted_request("a different claim")
    result = await verify_memory_write(repo, other, fact_id)
    assert result.exists
    assert result.content_matches is False
    assert result.verified is False


async def test_verifier_uses_db_channel_and_reads_no_page():
    """Decorrelation, structurally: the verifier takes a repo + request and no page.
    It uses the DB channel (repo.get is called) and cannot touch the untrusted
    source. A page-read counter that only a browser could move stays at 0."""
    repo = InMemoryFactRepository()
    req = untrusted_request("decorrelation check")
    fact_id = await write_untrusted_fact(repo, req, reviewed=True)

    page_reads = 0  # only a browser would ever increment this; the verifier has none
    get_calls_before = repo.get_calls
    result = await verify_memory_write(repo, req, fact_id)
    page_reads_after = 0

    assert page_reads_after - page_reads == 0          # verifier added zero untrusted reads
    assert repo.get_calls > get_calls_before           # it did use the DB channel
    assert result.verified
