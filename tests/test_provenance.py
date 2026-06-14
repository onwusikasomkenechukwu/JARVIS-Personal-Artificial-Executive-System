from jarvis.provenance import effective_level, escalate
from jarvis.requests import Action, ActionRequest, Provenance


def make(provenance: Provenance, level: int, action: Action = Action.READ) -> ActionRequest:
    return ActionRequest(action=action, provenance=provenance, trigger_source="t", level=level)


def test_user_direct_level3_unchanged():
    r = make(Provenance.USER_DIRECT, 3, Action.SEND_MESSAGE)
    assert escalate(r).level == 3
    assert effective_level(r) == 3


def test_untrusted_level3_capped_at_2():
    r = make(Provenance.UNTRUSTED_DERIVED, 3, Action.SEND_MESSAGE)
    assert escalate(r).level == 2
    assert effective_level(r) == 2


def test_untrusted_level1_escalated_to_2():
    r = make(Provenance.UNTRUSTED_DERIVED, 1)
    assert escalate(r).level == 2


def test_untrusted_level0_escalated_to_1():
    # escalated by at least one level, but still under the cap
    r = make(Provenance.UNTRUSTED_DERIVED, 0)
    assert escalate(r).level == 1


def test_escalate_is_pure():
    r = make(Provenance.UNTRUSTED_DERIVED, 3)
    escalate(r)
    assert r.level == 3  # original untouched
