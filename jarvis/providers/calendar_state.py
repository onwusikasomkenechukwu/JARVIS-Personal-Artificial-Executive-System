"""Calendar provider state-read — own calendar, with the invite injection boundary.

Reads the user's upcoming calendar events (primary calendar, a forward window) under a
read-only scope. Like `gmail_state`, the trust boundary is enforced in two independent
layers — but here there is a SECOND, sharper boundary that the mail read never had: a
calendar event the user was *invited to* by someone else is attacker-authorable content.

  1. SCOPE. OAuth scope is `calendar.readonly` only (see SCOPES). The provider enforces
     that this token cannot create, modify, or delete an event — read is the whole of its
     authority. Broadening it is a separate, re-consented change, never silent. The token
     lives in its OWN vault file, distinct from every Gmail token (decorrelated credentials).

  2. TYPE — and this is the load-bearing one. An external party who sends a calendar invite
     controls the event's title, description, and location. The description field on an
     externally-created invite is attacker-authored free text, exactly like an email body
     ("Meeting: your account is suspended, click bit.ly/xyz"). `CalendarEvent` makes the
     suppression STRUCTURAL, not a render-time choice: `notes` is only ever populated from
     `is_own=True` events. An external invite's description sets `has_external_description=True`
     and is otherwise dropped on the floor in the mapping — the digest renderer never has the
     attacker text to leak. Titles/locations of external invites are lower-risk but still
     attacker-set; they are carried as plain inert text (no link extraction, never treated as
     instruction).

The calendar-readonly token lives in the credential vault (outside the repo); the agent
code path holds a handle, never the raw token. Only the service builder here resolves it.

This module performs NO write of any kind: it calls `events().list()` and nothing else.
No insert/update/patch/delete method is ever referenced.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..config import get_logger
from . import gmail_state

log = get_logger("jarvis.calendar_state")

# --- The trust boundary, as constants the tests pin -------------------------

# Read-only. The provider enforces that this token cannot create/modify/delete an event.
# A future widening (e.g. calendar.events) must break the scope test deliberately.
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES: list[str] = [CALENDAR_READONLY_SCOPE]

# Its own vault file — never shared with any Gmail token. Decorrelated credentials.
CALENDAR_TOKEN_KEY = "calendar_readonly_token.json"

# Always the primary calendar; a forward window only (timeMin = now).
PRIMARY_CALENDAR = "primary"
DEFAULT_WINDOW_DAYS = 7
# Bounds a single read; ample for a one-week personal window.
DEFAULT_MAX_EVENTS = 250


# --- The typed event object -------------------------------------------------

class CalendarEvent(BaseModel):
    """One upcoming event. The structural rule is the security property: `notes` is
    populated ONLY from `is_own=True` events. An external invite's free-text description
    never reaches this object — it sets `has_external_description=True` and is dropped in
    the mapping. There is deliberately no raw `description` field for external events to
    leak through."""

    title: str
    start: datetime
    end: Optional[datetime] = None
    is_own: bool                      # the user created it vs was invited by someone else
    rsvp_status: Optional[str] = None  # accepted / tentative / declined / needsAction
    location: Optional[str] = None     # own: the user's own; external: inert attacker text
    has_external_description: bool = False  # an external invite carried a description
    notes: Optional[str] = None        # ONLY ever populated from own-created events
    # NO raw external-description field exists on this type. Do not add one.


# --- The state-read ---------------------------------------------------------

async def read_upcoming_events(
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    service: Any | None = None,
    self_email: Optional[str] = None,
    now: Optional[datetime] = None,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> list[CalendarEvent]:
    """Upcoming events on the PRIMARY calendar from now to now + `window_days`, as typed
    `CalendarEvent`s in start order. Recurring events are expanded (`singleEvents=True`).

    External-invite descriptions are excluded structurally during mapping — see the module
    docstring and `_to_event`. `service` is injected in tests; in production it is built
    from the calendar-readonly vault credentials. `self_email`/`now` are injectable for
    deterministic tests."""
    svc = service if service is not None else build_calendar_service()
    now = now or datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=window_days)).isoformat()

    raw_events: list[dict] = []
    page_token: Optional[str] = None
    while len(raw_events) < max_events:
        page_size = min(250, max_events - len(raw_events))
        token = page_token
        resp = await asyncio.to_thread(
            lambda tk=token, ps=page_size: svc.events()
            .list(
                calendarId=PRIMARY_CALENDAR,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=ps,
                pageToken=tk,
            )
            .execute()
        )
        raw_events.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    events = [_to_event(e, self_email) for e in raw_events]
    return [e for e in events if e is not None]


def _to_event(event: dict, self_email: Optional[str]) -> Optional[CalendarEvent]:
    """Map one Google event to a CalendarEvent, applying the description-suppression rule.

    The single security-load-bearing branch: a description is lifted into `notes` ONLY for
    an own-created event. For an external invite the description is recorded as a boolean
    presence flag and the text itself is discarded here — it never enters the object."""
    start = _parse_dt(event.get("start"))
    if start is None:
        return None  # an event with no resolvable start is unusable for a forward digest

    is_own = _is_own_event(event, self_email)
    description = event.get("description")
    has_description = bool(description and description.strip())

    if is_own:
        notes = description if has_description else None
        has_external_description = False
    else:
        # External invite: the description is attacker-authorable free text. Drop it; keep
        # only the fact that one existed. THIS is the structural suppression.
        notes = None
        has_external_description = has_description

    return CalendarEvent(
        title=event.get("summary") or "(no title)",
        start=start,
        end=_parse_dt(event.get("end")),
        is_own=is_own,
        rsvp_status=_rsvp_status(event),
        location=event.get("location") or None,
        has_external_description=has_external_description,
        notes=notes,
    )


def _is_own_event(event: dict, self_email: Optional[str]) -> bool:
    """Did the user create/organize this event, or were they invited to it? Google marks
    the user's own creatorship/organizership with a `self: true` flag — the primary signal.
    `self_email` is a fallback comparison (used in tests and if the flag is absent)."""
    creator = event.get("creator", {}) or {}
    organizer = event.get("organizer", {}) or {}
    if creator.get("self") or organizer.get("self"):
        return True
    if self_email:
        se = self_email.strip().lower()
        if creator.get("email", "").lower() == se or organizer.get("email", "").lower() == se:
            return True
    return False


def _rsvp_status(event: dict) -> Optional[str]:
    """The user's own RSVP responseStatus, from the attendee marked `self: true`. None if
    the user is not in the attendee list (e.g. a solo event they created)."""
    for a in event.get("attendees", []) or []:
        if a.get("self"):
            return a.get("responseStatus")
    return None


def _parse_dt(node: Optional[dict]) -> Optional[datetime]:
    """Parse a Google event start/end node. Timed events carry `dateTime` (RFC3339);
    all-day events carry `date` (YYYY-MM-DD, parsed as naive midnight). None if absent or
    malformed — we never guess a time."""
    if not node:
        return None
    val = node.get("dateTime") or node.get("date")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Credentials (resolved only at this execution boundary) -----------------

def build_calendar_service(vault=None, client_secret_dir=None) -> Any:
    """The calendar-readonly Gmail-API-style client, built from its OWN vault token. First
    use triggers a separate `calendar.readonly` consent; the token is stored apart from
    every Gmail token. Reuses the vault + installed-app OAuth flow from `gmail_state`
    (parameterised by scope + token_key — the same execution-boundary credential plumbing),
    then builds the Calendar v3 service."""
    from googleapiclient.discovery import build

    creds = gmail_state.load_gmail_credentials(
        vault=vault,
        client_secret_dir=client_secret_dir,
        scopes=SCOPES,
        token_key=CALENDAR_TOKEN_KEY,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
