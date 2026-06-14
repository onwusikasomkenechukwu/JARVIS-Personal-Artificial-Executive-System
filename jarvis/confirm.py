"""Out-of-band confirmation gate (milestone 4).

Any action at level >= 2 must be confirmed through a channel that is NOT the code
path that requested it. The requesting path can only *write* a pending request and
poll; the decision is made by a separate process:

    python -m jarvis.confirm list
    python -m jarvis.confirm approve <id>
    python -m jarvis.confirm deny <id>

Every prompt shows DIFF (what will change) and SOURCE (what triggered it), and is
flagged red when the trigger is untrusted-derived. Default-safe: a timeout resolves
to denial (no action), per Principle 7.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .config import get_logger, settings
from .provenance import effective_level
from .requests import ActionRequest, Provenance

log = get_logger("jarvis.confirm")

CONFIRM_REQUIRED_LEVEL = 2


@dataclass
class ConfirmationPrompt:
    id: str
    action: str
    level: int
    diff: str       # exactly what will change
    source: str     # what triggered it
    untrusted: bool  # flag red

    def render(self) -> str:
        flag = "   *** UNTRUSTED-DERIVED TRIGGER — RED ***" if self.untrusted else ""
        return (
            f"[confirm {self.id}] action={self.action} level={self.level}{flag}\n"
            f"  SOURCE: {self.source}\n"
            f"  DIFF:\n{self.diff}"
        )


class ConfirmationChannel(Protocol):
    async def request(self, prompt: ConfirmationPrompt) -> bool: ...


async def gate(request: ActionRequest, diff: str, channel: ConfirmationChannel) -> bool:
    """Return True only if the action is approved. Levels < 2 pass without prompting;
    levels >= 2 are routed to the out-of-band `channel`."""
    lvl = effective_level(request)
    if lvl < CONFIRM_REQUIRED_LEVEL:
        return True

    prompt = ConfirmationPrompt(
        id=uuid.uuid4().hex[:12],
        action=request.action.value,
        level=lvl,
        diff=diff,
        source=request.trigger_source,
        untrusted=request.provenance is Provenance.UNTRUSTED_DERIVED,
    )
    log.info("confirmation_requested", id=prompt.id, action=prompt.action, level=lvl, untrusted=prompt.untrusted, source=prompt.source)
    approved = await channel.request(prompt)
    log.info("confirmation_resolved", id=prompt.id, approved=approved)
    return approved


class FileConfirmationChannel:
    """Out-of-band via the filesystem. Writes <id>.request.json into the pending dir
    and waits for a SEPARATE process to drop <id>.approve or <id>.deny. The requesting
    path cannot create the decision file, so it cannot self-approve."""

    def __init__(self, pending_dir: str | None = None, timeout_s: float = 300, poll_s: float = 1.0) -> None:
        self.dir = Path(pending_dir or settings.pending_confirm_dir)
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    async def request(self, prompt: ConfirmationPrompt) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{prompt.id}.request.json").write_text(
            json.dumps(asdict(prompt), indent=2), encoding="utf-8"
        )
        approve = self.dir / f"{prompt.id}.approve"
        deny = self.dir / f"{prompt.id}.deny"
        deadline = time.monotonic() + self.timeout_s
        try:
            while time.monotonic() < deadline:
                if approve.exists():
                    return True
                if deny.exists():
                    return False
                await asyncio.sleep(self.poll_s)
            log.warning("confirmation_timeout_default_deny", id=prompt.id)
            return False  # default-safe
        finally:
            self._cleanup(prompt.id)

    def _cleanup(self, pid: str) -> None:
        for suffix in (".request.json", ".approve", ".deny"):
            p = self.dir / f"{pid}{suffix}"
            if p.exists():
                p.unlink()


def _cli(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="jarvis.confirm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show pending confirmation requests")
    ap = sub.add_parser("approve", help="approve a pending request")
    ap.add_argument("id")
    dp = sub.add_parser("deny", help="deny a pending request")
    dp.add_argument("id")
    args = parser.parse_args(argv)

    d = Path(settings.pending_confirm_dir)
    if args.cmd == "list":
        reqs = sorted(d.glob("*.request.json")) if d.exists() else []
        if not reqs:
            print("(no pending confirmations)")
        for f in reqs:
            print(f.read_text(encoding="utf-8"))
            print("-" * 40)
    elif args.cmd == "approve":
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{args.id}.approve").write_text("ok", encoding="utf-8")
        print(f"approved {args.id}")
    elif args.cmd == "deny":
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{args.id}.deny").write_text("no", encoding="utf-8")
        print(f"denied {args.id}")


if __name__ == "__main__":
    _cli()
