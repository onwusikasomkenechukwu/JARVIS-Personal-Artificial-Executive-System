"""Tool Router — default-deny allowlist, parameterized by provenance.

Ordering contract (Layer 3): provenance *pre-filters* the menu, it does not
post-check. The router reads the request's provenance-adjusted level first, then
offers only tools within that level. An untrusted-derived request never sees a
Level 3+ tool in the first place — escalation constrains what can be built, rather
than inspecting an action after it was built under attacker influence.
"""
from __future__ import annotations

from .provenance import effective_level
from .requests import Action, ActionRequest

# Each permitted tool declares its required authorization level (its max blast radius).
# Anything not in this map is denied by default.
TOOL_ALLOWLIST: dict[Action, int] = {
    Action.NAVIGATE: 0,
    Action.READ: 0,
    Action.FILL: 2,
    Action.CLICK: 2,
    Action.WRITE_MEMORY: 2,
    Action.SEND_MESSAGE: 3,
    Action.TRANSFER_FUNDS: 4,
}


def available_tools(request: ActionRequest) -> list[Action]:
    """The pre-filtered menu: only tools whose required level is within the
    request's provenance-adjusted level. Computed before any tool is selected."""
    lvl = effective_level(request)
    return [tool for tool, need in TOOL_ALLOWLIST.items() if need <= lvl]


def is_allowed(request: ActionRequest, action: Action | str | None = None) -> bool:
    """Whether `action` may be invoked for this request. Default-deny: unknown
    tools and tools above the provenance-adjusted level both return False."""
    action = action if action is not None else request.action
    if action not in TOOL_ALLOWLIST:  # default-deny for anything not explicitly listed
        return False
    return TOOL_ALLOWLIST[action] <= effective_level(request)
