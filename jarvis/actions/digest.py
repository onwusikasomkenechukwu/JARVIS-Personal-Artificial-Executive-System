"""The "plate" digest — a read-and-report over the user's OWN data only.

This is an observe-and-report capability, not an action: it reads the user's own calendar
and own sent mail and renders a digest the user reads. It takes NO action on the world —
no send, no write, no calendar modification. Everything here is Level 0/1 (read / report).
It lives under `jarvis.actions` for its CLI path, but unlike its siblings it composes no
send/confirm spine because it has no side effect to gate.

Two trust levels across three sources:

  * The user's own calendar events and own sent mail are own data (trusted-ish — the user
    authored them).
  * A calendar event the user was INVITED to is the untrusted surface. Its description is
    attacker-authorable, handled structurally in `calendar_state.CalendarEvent` (`notes` is
    only ever filled from own events; an external description becomes a boolean flag and is
    dropped). This module never undoes that: it renders the marker, never the text.

What "what's on my plate" means here is exactly own-calendar + own-sent, full stop. It
reads ZERO inbound mail — the sent summary lists with `labelIds=["SENT"]`, so inbound
messages are never even fetched. Things-people-are-waiting-on-from-inbound is a separate
future build with its own threat model and is deliberately absent.

    python -m jarvis.actions.digest --days 7
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..config import get_logger
from ..providers import calendar_state, gmail_state
from ..providers.calendar_state import CalendarEvent, read_upcoming_events

log = get_logger("jarvis.digest")

# Only the SENT label is listed — inbound mail is never fetched (read-zero-inbound).
SENT_LABEL = "SENT"
# Headers lifted for the sent summary. Metadata only; never a body part or snippet.
SENT_HEADERS: list[str] = ["To", "Subject", "Date"]
# Bounds the sent scan. Recent sends sit at the front of recency order, so a one-week
# window is comfortably covered; an extremely high-volume sender is bounded here.
DEFAULT_SENT_SCAN = 100
DEFAULT_WINDOW_DAYS = 7

# The renderer emits THIS for an external invite that carried a description — never the
# description text itself (which never reached the typed object to begin with).
# ASCII-only so it renders on any console (Windows cp1252 cannot encode em-dash/arrow).
EXTERNAL_DESCRIPTION_MARKER = "[external invite - description not shown]"


# --- Sent-mail summary (metadata scope; reuses gmail_state.scan_sent_headers) -

class SentSummary(BaseModel):
    """One sent message, metadata only. The ABSENCE of any body/snippet/content field is
    deliberate and is unit-tested: a sent summary cannot carry message content."""

    to: str
    subject: str
    date: str  # the raw RFC 2822 Date header, rendered inert
    # NO body, NO snippet, NO payload field exists on this type. Do not add one.


async def summarize_sent(
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    service: Any | None = None,
    max_scan: int = DEFAULT_SENT_SCAN,
    now: Optional[datetime] = None,
) -> list[SentSummary]:
    """Summarize the user's own SENT mail in the last `window_days`. Metadata scope only —
    To/Subject/Date headers, no bodies, no snippets.

    The `gmail.metadata` scope rejects the `q` parameter (it can match body text), so we
    cannot ask Gmail for `in:sent newer_than:7d` directly. Instead we list with
    `labelIds=["SENT"]` (a label filter, which the metadata scope permits) — so only sent
    messages are ever listed or fetched, never inbound — and filter the recent window
    client-side on the parsed Date header. `service` is injected in tests."""
    svc = service if service is not None else gmail_state.build_gmail_service()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    rows = await gmail_state.scan_sent_headers(
        svc, SENT_HEADERS, max_scan, label_ids=[SENT_LABEL]
    )

    out: list[SentSummary] = []
    for r in rows:
        if SENT_LABEL not in r.get("labels", []):
            continue  # belt-and-suspenders; the label filter already guarantees this
        h = r.get("headers", {})
        when = _parse_email_date(h.get("Date"))
        if when is not None and when < cutoff:
            continue
        out.append(
            SentSummary(to=h.get("To", ""), subject=h.get("Subject", ""), date=h.get("Date", ""))
        )
    return out


def _parse_email_date(value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 2822 Date header to an aware datetime (UTC if no tz). None if absent or
    unparseable — an unparseable date is kept (not silently dropped from the window)."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- The plate digest -------------------------------------------------------

class PlateDigest(BaseModel):
    """Own calendar + own sent mail, combined. Reads no inbound mail and carries nothing
    actionable — every field is inert report data."""

    generated_at: datetime
    window_days: int
    events: list[CalendarEvent] = Field(default_factory=list)
    sent: list[SentSummary] = Field(default_factory=list)


async def build_plate(
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    calendar_service: Any | None = None,
    gmail_service: Any | None = None,
    self_email: Optional[str] = None,
    now: Optional[datetime] = None,
) -> PlateDigest:
    """Combine upcoming calendar commitments and recent own-sent mail into one digest.
    Reads only the user's own data; reads zero inbound mail. Services are injected in tests
    and built from their own scoped vault tokens in production."""
    now = now or datetime.now(timezone.utc)
    events = await read_upcoming_events(
        window_days, service=calendar_service, self_email=self_email, now=now
    )
    sent = await summarize_sent(window_days, service=gmail_service, now=now)
    return PlateDigest(generated_at=now, window_days=window_days, events=events, sent=sent)


# --- Rendering (everything inert: no link following, no action affordances) ---

def render_plate(digest: PlateDigest) -> str:
    """Render the digest to readable, inert text. External-invite descriptions appear ONLY
    as `EXTERNAL_DESCRIPTION_MARKER`; their text is never available to render. No calendar
    or mail text is ever treated as an instruction or a followable link."""
    lines: list[str] = [
        "What's on your plate",
        f"  window:       next {digest.window_days} day(s)",
        f"  generated at: {digest.generated_at.isoformat()}",
        "  (own calendar + own sent mail only - no inbound mail read)",
        "",
        f"Upcoming commitments ({len(digest.events)})",
    ]
    if not digest.events:
        lines.append("  (nothing scheduled)")
    for e in digest.events:
        origin = "own" if e.is_own else "invited"
        when = _fmt_when(e)
        lines.append(f"  * {e.title}  [{origin}]")
        lines.append(f"      when: {when}")
        if e.rsvp_status:
            lines.append(f"      rsvp: {e.rsvp_status}")
        if e.location:
            lines.append(f"      location: {e.location}")
        if e.is_own and e.notes:
            lines.append(f"      notes: {e.notes}")
        if e.has_external_description:
            lines.append(f"      {EXTERNAL_DESCRIPTION_MARKER}")

    lines += ["", f"Recently sent ({len(digest.sent)})"]
    if not digest.sent:
        lines.append("  (nothing sent in window)")
    for s in digest.sent:
        lines.append(f"  * to {s.to}: {s.subject}")
        if s.date:
            lines.append(f"      sent: {s.date}")
    return "\n".join(lines)


def _fmt_when(e: CalendarEvent) -> str:
    start = e.start.isoformat()
    if e.end is not None:
        return f"{start} -> {e.end.isoformat()}"
    return start


# --- CLI --------------------------------------------------------------------

async def _amain(argv: list[str] | None = None) -> None:
    import argparse

    from ..config import configure_logging

    parser = argparse.ArgumentParser(prog="jarvis.actions.digest")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"forward calendar window / sent-mail lookback in days (default {DEFAULT_WINDOW_DAYS})",
    )
    args = parser.parse_args(argv)

    configure_logging()
    print("Building your plate (read-only: own calendar + own sent mail)...")
    print("First run opens a browser for the calendar.readonly consent (separate token).\n")
    digest = await build_plate(window_days=args.days)
    print(render_plate(digest))


if __name__ == "__main__":
    asyncio.run(_amain())
