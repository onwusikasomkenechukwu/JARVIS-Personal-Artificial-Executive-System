"""User-instructed email send — the first action that acts irreversibly on the world.

This is the first time the whole safety spine has to *compose* into one flow instead of
passing as separate unit tests:

    draft → PRE-SEND GUARD → OUT-OF-BAND CONFIRM → SEND → POST-SEND VERIFY

Scope is deliberately narrow: a send the user explicitly instructed (recipient/subject/
body from the CLI). Provenance is always USER_DIRECT — there is no code path that builds
a SendRequest from content JARVIS read. JARVIS-initiated send, and acting on untrusted
content, are the next (harder) build; the provenance gate gets its real test there.

Two design properties carry this build:

1. AT-MOST-ONCE UNDER RETRY (the single most important property). Gmail has no provider-
   side send idempotency, and the transport may retry a send that *succeeded server-side
   but timed out client-side*. The pre-send guard is the only thing preventing a double
   send, so it is re-checked before EVERY send attempt (`_attempt_send` calls `_guard`
   first). If a prior attempt actually delivered, the guard now sees the sent message and
   refuses the duplicate. Retry + guard re-check together = at-most-once. This is wired,
   not hoped for — see `test_send_email.py::test_retry_after_timeout_success_sends_once`.

   The idempotency key is the SendRequest's `content_hash`, embedded in an
   `X-Jarvis-Content-Hash` header WE set on the outbound MIME. The guard reads that header
   back off recent sent mail through the *metadata* scope (which cannot read bodies, and
   rejects `q`-search — see gmail_state). Matching a marker we authored, on the user's own
   outbound mail, needs no body access.

   For the guard to catch a timed-out-but-delivered send, its re-check must run AFTER that
   send becomes visible to the metadata read. The index-visibility gap was measured live
   at 0.36–0.61s; `MIN_RESEND_GAP_S` (the floor on the pre-retry wait) sits well above it,
   so a retry never queries inside the visibility window. Honest limit (documented, not
   fixed): there is still a check-then-send race between the guard passing and the send
   firing. It is safe for a single-user system sending one at a time; it is NOT safe for
   concurrent/autonomous sending, which is out of scope and must not be enabled here.

2. DECORRELATED SEND-AND-VERIFY (Principle 4 at the credential layer). The credential that
   sends and the credential that confirms-sent are different OAuth scopes with different
   tokens: `gmail.send` can only send and cannot read state; `gmail.metadata` can only
   read state and cannot send. If one token is compromised it cannot both forge the action
   and forge its own verification. They are never co-granted and live in separate vault
   files.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field, field_validator

from ..config import get_logger
from ..confirm import ConfirmationChannel, FileConfirmationChannel, gate
from ..providers import gmail_state
from ..providers.gmail_state import confirm_message_state
from ..requests import Action, ActionRequest, Level, Provenance
from ..router import is_allowed

log = get_logger("jarvis.send_email")

# --- The send credential: a SECOND scope + token, never co-granted with metadata -------

# Send-only. A restricted scope: it can send and nothing else — it cannot read state, so
# it cannot also forge the verification of its own send.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SEND_SCOPES: list[str] = [GMAIL_SEND_SCOPE]
# Stored separately from the metadata token (gmail_state.GMAIL_TOKEN_KEY). Two tokens.
GMAIL_SEND_TOKEN_KEY = "gmail_send_token.json"

# The idempotency marker we set on every outbound message and read back through metadata.
CONTENT_HASH_HEADER = "X-Jarvis-Content-Hash"

# How far back the idempotency guard looks (recent window; a just-sent or recently-sent
# duplicate is at the front of recency order). Bounds the metadata scan.
DEFAULT_GUARD_SCAN = 50
# Verification scans a tighter window: a genuine double-send produces two messages with
# the SAME content-hash, adjacent at the very front of recency order.
VERIFY_MAX_SCAN = 25

# Minimum wait before a retry re-checks the guard. Must exceed the measured index-
# visibility gap (the lag between Gmail accepting a send and the guard's metadata read
# seeing it) so a retry never queries inside that window and re-sends a message that
# already landed. Live measurement: 0.36–0.61s; this floor leaves comfortable margin.
MIN_RESEND_GAP_S = 3.0


# --- Typed request / result -------------------------------------------------

class SendRequest(BaseModel):
    """A user-instructed send. There is intentionally NO `provenance` field and no way to
    pass one: every SendRequest is USER_DIRECT by construction (see `send_email`). The
    `content_hash` is the idempotency key."""

    to: str
    subject: str
    body: str

    @field_validator("to")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError(f"recipient does not look like an email address: {v!r}")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Stable hash of normalised recipient + subject + body. Recipient is
        case-folded/trimmed; subject trimmed; body kept verbatim (whitespace is content)."""
        norm = f"{self.to.strip().lower()}\n{self.subject.strip()}\n{self.body}"
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class ConfirmationRecord(BaseModel):
    approved: bool
    out_of_band: bool = True
    source: str                  # trigger_source, e.g. "user"
    provenance: str              # USER_DIRECT here
    diff: str                    # the full email shown for approval (no truncation)


class SendResult(BaseModel):
    sent: bool                                   # a message verifiably exists as SENT
    verified: bool = False                       # confirmed via the metadata credential
    already_sent: bool = False                   # a prior copy existed before we tried
    gmail_id: Optional[str] = None
    message_id: Optional[str] = None
    content_hash: Optional[str] = None
    duplicate_count: int = 0                     # SENT messages bearing our Message-ID
    confirmation: Optional[ConfirmationRecord] = None
    note: str = ""


class _GuardOutcome(BaseModel):
    already_sent: bool
    gmail_id: Optional[str] = None
    reason: str = ""


class _VerifyOutcome(BaseModel):
    verified: bool
    gmail_id: Optional[str] = None
    duplicate_count: int = 0


# --- Stage 1: draft (build the MIME + a stable Message-ID, once per call) ----

def build_mime(req: SendRequest) -> tuple[str, str]:
    """Return (rfc_message_id, raw_base64url). We set a valid Message-ID for RFC
    correctness, but DO NOT rely on it downstream: a live send proved Gmail rewrites a
    client-supplied Message-ID. The stable handle for both the idempotency guard and
    verification is the `X-Jarvis-Content-Hash` header, which Gmail preserves (verified
    live). The content-hash is fixed by the request, so it is identical across every retry
    attempt and across re-invocations with the same content."""
    msg = EmailMessage()
    msg["To"] = req.to
    msg["Subject"] = req.subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id  # RFC-valid; Gmail replaces it on send (do not depend on it)
    # Our own idempotency/verification marker, read back through the metadata scope
    # (a header WE authored on the user's own outbound mail — never a body read). Preserved.
    msg[CONTENT_HASH_HEADER] = req.content_hash
    msg.set_content(req.body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return message_id, raw


# --- Stage 2/4: the pre-send idempotency guard (metadata credential) ---------

async def _guard(state_service: Any, req: SendRequest, *, guard_scan: int) -> _GuardOutcome:
    """Has a message carrying this exact `content_hash` already been sent (within the
    recent scan window)? Reads only the `X-Jarvis-Content-Hash` header we set, off the
    user's own SENT mail, through the metadata scope. Refusal-by-content-hash is exact —
    no fuzzy subject/recipient match, no body read."""
    scanned = await gmail_state.scan_sent_headers(state_service, [CONTENT_HASH_HEADER], guard_scan)
    for m in scanned:
        if "SENT" in m["labels"] and m["headers"].get(CONTENT_HASH_HEADER) == req.content_hash:
            return _GuardOutcome(
                already_sent=True,
                gmail_id=m["id"],
                reason="a message with this content-hash already shows as SENT (idempotency guard)",
            )
    return _GuardOutcome(already_sent=False)


# --- Stage 4: send (gmail.send credential only), retry-safe by guard re-check -

def _is_retryable(exc: BaseException) -> bool:
    """A transport-level failure where the send may or may not have reached Gmail — the
    case the guard exists for. Permanent 4xx (bad request, auth) are NOT retryable."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "resp", None), "status", None
            )
            return status in (429, 500, 502, 503, 504)
    except Exception:
        pass
    return False


async def _execute_send(send_service: Any, raw: str) -> dict:
    return await asyncio.to_thread(
        lambda: send_service.users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )


# --- Stage 5: post-send verification (metadata credential, decorrelated) -----

async def verify_sent(
    state_service: Any,
    content_hash: str,
    *,
    verify_scan: int = VERIFY_MAX_SCAN,
    attempts: int = 1,
    delay_s: float = 0.0,
) -> _VerifyOutcome:
    """Through the METADATA credential (never the send credential), confirm a SENT message
    carrying this `content_hash` exists, and count how many do. >1 ⇒ a double-send the
    guard should have prevented — detected here as a loud backstop. Polls a few times to
    ride out brief indexing lag after a real send.

    Why content-hash and not the generated Message-ID: a live send proved Gmail REWRITES a
    client-supplied Message-ID (our `<…@host>` became `<…@mail.gmail.com>`), so resolving
    by it finds nothing. The `X-Jarvis-Content-Hash` header WE set is preserved (verified
    live), so it is the stable, decorrelated handle — it depends on neither the send
    response nor Gmail's Message-ID handling."""
    for i in range(max(1, attempts)):
        scanned = await gmail_state.scan_sent_headers(
            state_service, [CONTENT_HASH_HEADER], verify_scan
        )
        matches = [
            m for m in scanned
            if "SENT" in m["labels"] and m["headers"].get(CONTENT_HASH_HEADER) == content_hash
        ]
        if matches:
            return _VerifyOutcome(
                verified=True, gmail_id=matches[0]["id"], duplicate_count=len(matches)
            )
        if i + 1 < attempts:
            await asyncio.sleep(delay_s)
    return _VerifyOutcome(verified=False, gmail_id=None, duplicate_count=0)


# --- Service builders (the execution boundary; agent never sees raw tokens) --

def build_gmail_send_service(vault=None, client_secret_dir=None) -> Any:
    """The send-scoped Gmail client, built from its OWN token. First use triggers a
    separate `gmail.send` consent; the token is stored apart from the metadata token."""
    return gmail_state.build_gmail_service(
        vault=vault,
        client_secret_dir=client_secret_dir,
        scopes=SEND_SCOPES,
        token_key=GMAIL_SEND_TOKEN_KEY,
    )


def build_gmail_state_service(vault=None, client_secret_dir=None) -> Any:
    """The metadata-scoped client used by the guard and verifier (the default scope)."""
    return gmail_state.build_gmail_service(vault=vault, client_secret_dir=client_secret_dir)


# --- The pipeline -----------------------------------------------------------

def _render_diff(req: SendRequest) -> str:
    """The COMPLETE email shown for out-of-band approval. No truncation — a confirmation
    that hides part of what sends is worse than none."""
    return (
        f"To:      {req.to}\n"
        f"Subject: {req.subject}\n"
        f"Content-Hash: {req.content_hash}\n"
        f"Body:\n{req.body}"
    )


async def send_email(
    req: SendRequest,
    *,
    send_service: Any | None = None,
    state_service: Any | None = None,
    channel: ConfirmationChannel | None = None,
    max_send_attempts: int = 3,
    guard_scan: int = DEFAULT_GUARD_SCAN,
    verify_scan: int = VERIFY_MAX_SCAN,
    send_backoff_s: float = 0.5,
    min_resend_gap_s: float = MIN_RESEND_GAP_S,
    verify_attempts: int = 5,
    verify_delay_s: float = 1.0,
) -> SendResult:
    """Run the full send pipeline. Services are injected in tests; in production they are
    built from their own scoped vault tokens. Returns a typed SendResult; never raises for
    an expected refusal (denied/timeout/already-sent), only for programming errors."""
    # Provenance is USER_DIRECT, always, by construction — there is no parameter to pass
    # any other provenance, and no path here that reads untrusted content.
    action = ActionRequest(
        action=Action.SEND_MESSAGE,
        args={"to": req.to, "subject": req.subject, "content_hash": req.content_hash},
        provenance=Provenance.USER_DIRECT,
        trigger_source="user",
        level=int(Level.COMMUNICATION),  # Level 3
    )

    # Spine, layer 1: authorization (default-deny allowlist). SEND_MESSAGE is Level 3.
    if not is_allowed(action):
        return SendResult(sent=False, content_hash=req.content_hash,
                          note="authorization denied by tool router")

    # Stage 1: draft.
    message_id, raw = build_mime(req)
    state_service = state_service or build_gmail_state_service()

    # Stage 2: pre-send guard — don't even ask the user if it already went out.
    pre = await _guard(state_service, req, guard_scan=guard_scan)
    if pre.already_sent:
        log.info("send_skipped_already_sent", content_hash=req.content_hash, gmail_id=pre.gmail_id)
        outcome = await verify_sent(state_service, req.content_hash, verify_scan=verify_scan)
        return SendResult(
            sent=True, verified=False, already_sent=True, gmail_id=pre.gmail_id,
            message_id=message_id, content_hash=req.content_hash,
            duplicate_count=outcome.duplicate_count, note=pre.reason,
        )

    # Stage 3: out-of-band confirmation (Level 3 gate). MUST be a different process/path;
    # the requesting path cannot self-approve. Shows the full email; default-safe on
    # timeout/no-response (gate returns False).
    channel = channel or FileConfirmationChannel()
    diff = _render_diff(req)
    approved = await gate(action, diff, channel)
    conf = ConfirmationRecord(
        approved=approved, source=action.trigger_source,
        provenance=action.provenance.value, diff=diff,
    )
    if not approved:
        log.info("send_not_approved", content_hash=req.content_hash)
        return SendResult(
            sent=False, message_id=message_id, content_hash=req.content_hash,
            confirmation=conf, note="not approved out-of-band (default-safe: no send)",
        )

    # Stage 4: send. Acquire the send credential only now (approved), and re-run the guard
    # before EVERY attempt so a timed-out-but-succeeded send is never re-sent.
    send_service = send_service or build_gmail_send_service()
    sent_id: Optional[str] = None
    guard_suppressed = False
    last_err: Optional[str] = None

    for attempt in range(1, max_send_attempts + 1):
        g = await _guard(state_service, req, guard_scan=guard_scan)
        if g.already_sent:
            # A prior attempt actually delivered (server-side success that we saw as a
            # timeout). At-most-once: suppress the re-send.
            sent_id = g.gmail_id
            guard_suppressed = attempt > 1
            log.info("send_retry_suppressed_by_guard", attempt=attempt, gmail_id=g.gmail_id)
            break
        try:
            resp = await _execute_send(send_service, raw)
            sent_id = resp.get("id")
            log.info("send_call_returned", attempt=attempt, gmail_id=sent_id)
            break
        except Exception as e:  # noqa: BLE001 — classify, then decide
            last_err = f"{type(e).__name__}: {e}"
            if not _is_retryable(e):
                log.error("send_failed_non_retryable", attempt=attempt, error=last_err)
                return SendResult(
                    sent=False, message_id=message_id, content_hash=req.content_hash,
                    confirmation=conf, note=f"send failed (non-retryable): {last_err}",
                )
            log.warning("send_retryable_error", attempt=attempt, error=last_err)
            if attempt < max_send_attempts:
                # The wait before re-checking the guard MUST exceed the index-visibility
                # gap — the window between Gmail accepting a send and that send becoming
                # visible to the guard's metadata read. A retry that re-checks inside that
                # window would see nothing and re-send a message that already landed. Live
                # measurement put the gap at 0.36–0.61s; the floor (MIN_RESEND_GAP_S) sits
                # well above it with margin, so the guard never out-runs visibility.
                await asyncio.sleep(max(min_resend_gap_s, send_backoff_s * attempt))
            # loop continues → guard is re-checked before the next attempt

    # Stage 5: verify through the metadata credential (decorrelated from the send),
    # keyed on the content-hash marker (Gmail rewrites client Message-IDs — see verify_sent).
    outcome = await verify_sent(
        state_service, req.content_hash, verify_scan=verify_scan,
        attempts=verify_attempts, delay_s=verify_delay_s,
    )

    note = ""
    if guard_suppressed:
        note = "at-most-once: a retry was suppressed by the guard after a prior attempt succeeded"
    elif sent_id is None:
        note = f"all {max_send_attempts} attempts failed; last error: {last_err}"

    if outcome.duplicate_count > 1:
        note = (f"DOUBLE-SEND DETECTED: {outcome.duplicate_count} sent messages carry this "
                f"content-hash — the guard should have prevented this. " + note).strip()
        log.error("double_send_detected", content_hash=req.content_hash, count=outcome.duplicate_count)

    return SendResult(
        sent=outcome.duplicate_count >= 1,
        verified=outcome.verified,
        gmail_id=outcome.gmail_id or sent_id,
        message_id=message_id,
        content_hash=req.content_hash,
        duplicate_count=outcome.duplicate_count,
        confirmation=conf,
        note=note,
    )


# --- CLI --------------------------------------------------------------------

def _render_result(r: SendResult) -> str:
    lines = [
        "Send result",
        f"  sent:            {r.sent}",
        f"  verified:        {r.verified}",
        f"  already_sent:    {r.already_sent}",
        f"  gmail_id:        {r.gmail_id}",
        f"  message_id:      {r.message_id}",
        f"  duplicate_count: {r.duplicate_count}",
    ]
    if r.confirmation is not None:
        lines.append(f"  confirmed:       {r.confirmation.approved} "
                     f"(source={r.confirmation.source}, provenance={r.confirmation.provenance})")
    if r.note:
        lines.append(f"  note:            {r.note}")
    return "\n".join(lines)


async def _amain(argv: list[str] | None = None) -> None:
    import argparse

    from ..config import configure_logging

    parser = argparse.ArgumentParser(prog="jarvis.actions.send_email")
    parser.add_argument("--to", required=True, help="recipient email address")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--max-scan", type=int, default=DEFAULT_GUARD_SCAN,
                        help=f"idempotency-guard scan window (default {DEFAULT_GUARD_SCAN})")
    args = parser.parse_args(argv)

    configure_logging()
    req = SendRequest(to=args.to, subject=args.subject, body=args.body)

    print("Out-of-band confirmation required (Level 3). In a SEPARATE terminal:")
    print("  python -m jarvis.confirm list           # review the FULL email + note its id")
    print("  python -m jarvis.confirm approve <id>    # or: deny <id>")
    print("Waiting for approval (default-safe: no approval within the timeout = no send)...\n")

    result = await send_email(req, guard_scan=args.max_scan)
    print(_render_result(result))


if __name__ == "__main__":
    asyncio.run(_amain())
