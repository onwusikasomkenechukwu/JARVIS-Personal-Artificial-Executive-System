"""Milestone 4 acceptance: out-of-band confirmation gate."""
from jarvis.confirm import ConfirmationPrompt, gate
from jarvis.requests import Action, ActionRequest, Provenance


class RecordingChannel:
    """A stand-in for the out-of-band channel. Records the prompt it was shown and
    returns a fixed decision."""

    def __init__(self, decision: bool) -> None:
        self.decision = decision
        self.prompts: list[ConfirmationPrompt] = []

    async def request(self, prompt: ConfirmationPrompt) -> bool:
        self.prompts.append(prompt)
        return self.decision


def req(action: Action, provenance: Provenance, level: int) -> ActionRequest:
    return ActionRequest(action=action, provenance=provenance, trigger_source="https://x.test", level=level)


async def test_level_below_2_does_not_prompt():
    ch = RecordingChannel(decision=False)
    r = req(Action.READ, Provenance.USER_DIRECT, 0)
    assert await gate(r, diff="(none)", channel=ch) is True
    assert ch.prompts == []  # gate auto-passed without going out-of-band


async def test_level2_blocks_until_channel_approves():
    ch = RecordingChannel(decision=True)
    r = req(Action.WRITE_MEMORY, Provenance.USER_DIRECT, 2)
    assert await gate(r, diff="write fact X", channel=ch) is True
    assert len(ch.prompts) == 1


async def test_level2_denied_when_channel_denies():
    ch = RecordingChannel(decision=False)
    r = req(Action.WRITE_MEMORY, Provenance.USER_DIRECT, 2)
    assert await gate(r, diff="write fact X", channel=ch) is False


async def test_untrusted_trigger_is_flagged_red():
    ch = RecordingChannel(decision=True)
    # an untrusted level-1 request escalates to 2 and therefore must be confirmed
    r = req(Action.WRITE_MEMORY, Provenance.UNTRUSTED_DERIVED, 1)
    await gate(r, diff="write fact X", channel=ch)
    assert len(ch.prompts) == 1
    assert ch.prompts[0].untrusted is True
    assert "RED" in ch.prompts[0].render()
