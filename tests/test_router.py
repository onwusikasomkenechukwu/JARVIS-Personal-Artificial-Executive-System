from jarvis.requests import Action, ActionRequest, Provenance
from jarvis.router import available_tools, is_allowed


def req(provenance: Provenance, level: int, action: Action = Action.READ) -> ActionRequest:
    return ActionRequest(action=action, provenance=provenance, trigger_source="t", level=level)


def test_untrusted_cannot_select_level3_tool_even_by_name():
    r = req(Provenance.UNTRUSTED_DERIVED, 3, Action.SEND_MESSAGE)
    assert is_allowed(r, Action.SEND_MESSAGE) is False
    assert Action.SEND_MESSAGE not in available_tools(r)


def test_user_direct_can_select_level3_tool():
    r = req(Provenance.USER_DIRECT, 3, Action.SEND_MESSAGE)
    assert is_allowed(r, Action.SEND_MESSAGE) is True
    assert Action.SEND_MESSAGE in available_tools(r)


def test_unknown_tool_denied_by_default():
    r = req(Provenance.USER_DIRECT, 4)
    assert is_allowed(r, "exfiltrate_secrets") is False


def test_untrusted_read_still_allowed():
    r = req(Provenance.UNTRUSTED_DERIVED, 0, Action.READ)
    assert is_allowed(r, Action.READ) is True


def test_untrusted_menu_excludes_high_level_tools():
    r = req(Provenance.UNTRUSTED_DERIVED, 4, Action.TRANSFER_FUNDS)
    menu = available_tools(r)
    assert Action.TRANSFER_FUNDS not in menu
    assert Action.SEND_MESSAGE not in menu
    assert Action.READ in menu      # level 0 ok
    assert Action.FILL in menu      # level 2 ok (cap)
