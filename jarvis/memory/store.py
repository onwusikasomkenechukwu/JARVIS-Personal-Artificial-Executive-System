"""Fact storage with provenance + expiration.

The policy logic (expiry computation, staleness, the untrusted-write rule) is pure
and unit-tested without a database. FactStore wires that logic to asyncpg. A stale
fact is *flagged*, never deleted — a true fact that goes stale must not silently
vanish, but it must also not be trusted as current.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from ..config import settings
from ..requests import Provenance


class FactType(str, Enum):
    VOLATILE = "volatile"  # "user works at X" — expires, must be reconfirmed
    STABLE = "stable"      # "user's birth year" — effectively non-expiring


class UntrustedWriteRejected(Exception):
    """Raised when untrusted-source content is written to durable memory without review."""


# --- Pure policy functions (no DB) -----------------------------------------

def compute_expiry(
    fact_type: FactType,
    created_at: datetime,
    volatile_ttl_seconds: int | None = None,
) -> Optional[datetime]:
    if fact_type == FactType.STABLE:
        return None
    ttl = settings.volatile_ttl_seconds if volatile_ttl_seconds is None else volatile_ttl_seconds
    return created_at + timedelta(seconds=ttl)


def is_stale(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now > expires_at


def check_write_allowed(trust_label: Provenance, reviewed: bool) -> None:
    """Untrusted-source content cannot write to durable semantic memory without review."""
    if trust_label is Provenance.UNTRUSTED_DERIVED and not reviewed:
        raise UntrustedWriteRejected(
            "UNTRUSTED_DERIVED fact requires review=True before writing to durable memory"
        )


@dataclass
class Fact:
    content: str
    source: str
    trust_label: Provenance
    fact_type: FactType
    created_at: datetime
    expires_at: Optional[datetime]
    reviewed: bool = False
    id: Optional[int] = None

    @property
    def stale(self) -> bool:
        return is_stale(self.expires_at)


# --- DB-backed store -------------------------------------------------------

class FactStore:
    def __init__(self, conn) -> None:
        self._conn = conn  # an asyncpg connection

    async def write_fact(
        self,
        content: str,
        source: str,
        trust_label: Provenance,
        fact_type: FactType,
        reviewed: bool = False,
        now: Optional[datetime] = None,
    ) -> int:
        check_write_allowed(trust_label, reviewed)
        created = now or datetime.now(timezone.utc)
        expires = compute_expiry(fact_type, created)
        row = await self._conn.fetchrow(
            """
            INSERT INTO facts (content, source, trust_label, fact_type, created_at, expires_at, stale, reviewed)
            VALUES ($1, $2, $3, $4, $5, $6, false, $7)
            RETURNING id
            """,
            content, source, trust_label.value, fact_type.value, created, expires, reviewed,
        )
        return row["id"]

    async def read_fact(self, fact_id: int) -> Optional[Fact]:
        row = await self._conn.fetchrow("SELECT * FROM facts WHERE id = $1", fact_id)
        if row is None:
            return None
        fact = Fact(
            id=row["id"],
            content=row["content"],
            source=row["source"],
            trust_label=Provenance(row["trust_label"]),
            fact_type=FactType(row["fact_type"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            reviewed=row["reviewed"],
        )
        # Flag staleness on read rather than deleting.
        if fact.stale and not row["stale"]:
            await self._conn.execute("UPDATE facts SET stale = true WHERE id = $1", fact_id)
        return fact
