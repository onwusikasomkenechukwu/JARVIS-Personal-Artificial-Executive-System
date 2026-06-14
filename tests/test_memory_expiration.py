from datetime import datetime, timedelta, timezone

import pytest

from jarvis.memory.store import (
    FactType,
    UntrustedWriteRejected,
    check_write_allowed,
    compute_expiry,
    is_stale,
)
from jarvis.requests import Provenance


def test_volatile_fact_past_interval_reads_stale():
    created = datetime.now(timezone.utc) - timedelta(seconds=10)
    expires = compute_expiry(FactType.VOLATILE, created, volatile_ttl_seconds=1)
    assert expires is not None
    assert is_stale(expires) is True


def test_stable_fact_does_not_expire():
    created = datetime.now(timezone.utc)
    expires = compute_expiry(FactType.STABLE, created)
    assert expires is None
    assert is_stale(expires) is False


def test_volatile_fresh_fact_not_stale():
    created = datetime.now(timezone.utc)
    expires = compute_expiry(FactType.VOLATILE, created, volatile_ttl_seconds=3600)
    assert is_stale(expires) is False


def test_untrusted_write_without_review_rejected():
    with pytest.raises(UntrustedWriteRejected):
        check_write_allowed(Provenance.UNTRUSTED_DERIVED, reviewed=False)


def test_untrusted_write_with_review_allowed():
    check_write_allowed(Provenance.UNTRUSTED_DERIVED, reviewed=True)  # must not raise


def test_user_direct_write_allowed_without_review():
    check_write_allowed(Provenance.USER_DIRECT, reviewed=False)  # must not raise
