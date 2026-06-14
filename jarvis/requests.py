"""The typed request object — the security-critical artifact of Phase 1.

Every action JARVIS takes is an ActionRequest carrying its provenance and trigger
source. Provenance travels with the request from the moment of ingestion so the
router and confirmation gate can pre-filter on it (see provenance.py, router.py).
"""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field


class Provenance(str, Enum):
    """Where a request originated. Only USER_DIRECT may carry full authority;
    anything derived from content JARVIS *read* is UNTRUSTED_DERIVED."""

    USER_DIRECT = "USER_DIRECT"
    UNTRUSTED_DERIVED = "UNTRUSTED_DERIVED"


class Level(IntEnum):
    """Authorization levels (Phase 1 uses 0–4; Level 5 emergency is out of scope)."""

    READ_ONLY = 0          # read files, navigate, extract
    DRAFTING = 1           # drafts / recommendations
    NON_DESTRUCTIVE = 2    # form fill, click, write memory — single confirmation
    COMMUNICATION = 3      # send message / publish — strong confirmation
    FINANCIAL_PHYSICAL = 4  # money / physical — multi-factor, out-of-band


class Action(str, Enum):
    # Level 0 — read-only
    NAVIGATE = "navigate"
    READ = "read"
    # Level 2 — non-destructive interaction
    FILL = "fill"
    CLICK = "click"
    WRITE_MEMORY = "write_memory"
    # Level 3 — communication / publishing
    SEND_MESSAGE = "send_message"
    # Level 4 — financial / physical
    TRANSFER_FUNDS = "transfer_funds"


# Untrusted-derived requests can never auto-exceed this, no matter what content claims.
UNTRUSTED_LEVEL_CAP: int = int(Level.NON_DESTRUCTIVE)  # 2


class ActionRequest(BaseModel):
    """A typed, provenance-carrying request. This is the only object that crosses
    from the untrusted-read side toward execution; it carries everything the router
    and verifier need and nothing that requires interpreting prose."""

    action: Action
    args: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    trigger_source: str  # a url, a file path, or "user"
    level: int = Field(default=0, ge=0, le=4)
