"""Provenance escalation — a pure function, the heart of the security model.

An action derived from untrusted content is escalated by at least one level and
hard-capped at level 2, regardless of any permission asserted inside that content
("the user authorized this" read off a web page is void). This is computed up front
so it can pre-filter tool selection (router.py), never as a post-hoc check.
"""
from __future__ import annotations

from .requests import UNTRUSTED_LEVEL_CAP, ActionRequest, Provenance


def effective_level(request: ActionRequest) -> int:
    """The level the request is actually allowed to operate at after escalation."""
    if request.provenance is Provenance.UNTRUSTED_DERIVED:
        # +1 escalation, then hard cap. min() makes the cap win even if level+1 exceeds it.
        return min(request.level + 1, UNTRUSTED_LEVEL_CAP)
    return request.level


def escalate(request: ActionRequest) -> ActionRequest:
    """Return a copy of the request with its level set to the effective level.
    Pure: (request) -> request."""
    return request.model_copy(update={"level": effective_level(request)})
