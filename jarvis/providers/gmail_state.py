"""Gmail provider state-read — the first decorrelated ground-truth channel.

Confirms a message shows as *sent* in Gmail, reading ONLY provider state (labels and
the user's own headers), never message content. The state/content boundary is enforced
in two independent layers:

  1. SCOPE. OAuth scope is `gmail.metadata` only (see SCOPES). That scope grants labels
     and headers and *cannot* return a body — the provider enforces the boundary at the
     API. Broadening it is a separate, re-consented change, never silent.
  2. TYPE. `MessageState` carries only state fields and structurally cannot hold a body,
     snippet, or payload. Belt and suspenders; the belt is Google's.

A real consequence of layer 1: the `gmail.metadata` scope REJECTS the `q` search
parameter ("Metadata scope does not support 'q' parameter") — `q` can match body text,
so the metadata scope forbids it. We therefore resolve the RFC Message-ID the
metadata-compatible way: list messages (most recent first) and match each candidate's
`Message-Id` header client-side, reading only that one routing header per skipped
message and never any content. The cost is O(position of the target in recency order),
bounded by `max_scan`; for the verifier's case (a just-sent message) the match is near
the front. Widening to `gmail.readonly` to get `q` back is explicitly NOT done.

The metadata-scoped token lives in the credential vault (outside the repo). The agent
code path holds a handle, never the raw token; only credential loading here resolves it.

This build constructs the trusted channel in isolation. It performs no send, reads no
body, and does not yet verify any side-effecting action — a future send action will call
`confirm_message_state` as its ground-truth check.

    python -m jarvis.providers.gmail_state --rfc-id "<CAKs...@mail.gmail.com>"
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..config import get_logger, settings
from ..vault import CredentialVault, SecretHandle

log = get_logger("jarvis.gmail_state")

# --- The trust boundary, as constants the tests pin -------------------------

# Metadata only. Grants labels + headers; the provider rejects body/raw reads under it.
# A future widening to gmail.readonly et al. must break the scope test deliberately.
GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
SCOPES: list[str] = [GMAIL_METADATA_SCOPE]

# The only headers we lift into state. These are provider/user-set on the user's own
# sent mail (not attacker-authored). Nothing else — and never a body part — is read.
STATE_HEADERS: tuple[str, ...] = ("From", "To", "Subject", "Date")

# `get` MUST use this format. "full"/"raw" would pull payload/body; metadata returns
# only labelIds and headers, and the metadata scope would reject the broader formats.
MESSAGE_FORMAT = "metadata"

# The routing header we match on during resolution (the only header read off messages
# we then skip). Never content.
MESSAGE_ID_HEADER = "Message-Id"

# Default cap on how many messages resolution will examine before giving up. Resolution
# is most-recent-first, so a just-sent message is found almost immediately; the cap just
# bounds the worst case for an old or absent id.
DEFAULT_MAX_SCAN = 250

# Vault key for the stored metadata-scoped token.
GMAIL_TOKEN_KEY = "gmail_metadata_token.json"


# --- The typed state object -------------------------------------------------

class MessageState(BaseModel):
    """Provider state for one message. The ABSENCE of a body/snippet/payload field is
    load-bearing: this type structurally cannot carry message content. A future
    content-read gets its own type with UNTRUSTED_DERIVED handling — it does not extend
    this one."""

    exists: bool
    gmail_id: Optional[str] = None        # resolved internal id, if found
    labels: list[str] = Field(default_factory=list)   # e.g. ["SENT", "INBOX", ...]
    headers: dict[str, str] = Field(default_factory=dict)  # ONLY From/To/Subject/Date
    is_sent: bool = False                 # convenience: "SENT" in labels
    # NO body, NO snippet, NO payload field exists on this type. Do not add one.


# --- The state-read ---------------------------------------------------------

async def confirm_message_state(
    rfc_message_id: str,
    *,
    service: Any | None = None,
    max_scan: int = DEFAULT_MAX_SCAN,
) -> MessageState:
    """Confirm the provider state of the message with RFC 2822 Message-ID `rfc_message_id`.

    Resolves the RFC Message-ID to Gmail's internal id (by header match — see module
    docstring; `q` is unavailable under the metadata scope), then reads metadata-only
    state. Returns `exists=False` if nothing matches within `max_scan` messages.
    `service` is injected in tests; in production it is built from the metadata-scoped
    vault credentials.
    """
    svc = service if service is not None else build_gmail_service()

    gmail_id = await _resolve_internal_id(svc, rfc_message_id, max_scan)
    if gmail_id is None:
        return MessageState(exists=False)

    # format=metadata + an explicit header allowlist: the provider returns labelIds and
    # only these headers — no payload body, no matter what we do next.
    msg = await asyncio.to_thread(
        lambda: svc.users()
        .messages()
        .get(
            userId="me",
            id=gmail_id,
            format=MESSAGE_FORMAT,
            metadataHeaders=list(STATE_HEADERS),
        )
        .execute()
    )

    labels = list(msg.get("labelIds", []) or [])
    headers = _extract_state_headers(msg.get("payload", {}).get("headers", []))
    # NB: msg may carry a "snippet" field even under metadata format. It is body-derived
    # content; we deliberately never read it into MessageState.
    return MessageState(
        exists=True,
        gmail_id=gmail_id,
        labels=labels,
        headers=headers,
        is_sent="SENT" in labels,
    )


async def _resolve_internal_id(
    service: Any, rfc_message_id: str, max_scan: int
) -> Optional[str]:
    """Map an RFC 2822 Message-ID to Gmail's internal hex id WITHOUT `q` (forbidden under
    the metadata scope). Lists messages most-recent-first and matches each candidate's
    `Message-Id` header, reading only that one routing header per skipped message — never
    content. Returns None if no message matches within `max_scan` examined."""
    target = _normalize_message_id(rfc_message_id)
    scanned = 0
    page_token: Optional[str] = None

    while scanned < max_scan:
        page_size = min(100, max_scan - scanned)
        token = page_token  # bind for the closure below
        resp = await asyncio.to_thread(
            lambda: service.users()
            .messages()
            .list(userId="me", maxResults=page_size, pageToken=token)
            .execute()
        )
        for m in resp.get("messages", []) or []:
            scanned += 1
            header_value = await _message_id_header(service, m["id"])
            if header_value is not None and _normalize_message_id(header_value) == target:
                return m["id"]
            if scanned >= max_scan:
                break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return None


async def _message_id_header(service: Any, gmail_id: str) -> Optional[str]:
    """Read ONLY the Message-Id header of one message (metadata format). No content."""
    msg = await asyncio.to_thread(
        lambda: service.users()
        .messages()
        .get(
            userId="me",
            id=gmail_id,
            format=MESSAGE_FORMAT,
            metadataHeaders=[MESSAGE_ID_HEADER],
        )
        .execute()
    )
    for h in msg.get("payload", {}).get("headers", []) or []:
        if h.get("name", "").lower() == MESSAGE_ID_HEADER.lower():
            return h.get("value")
    return None


def _normalize_message_id(value: str) -> str:
    """Compare Message-IDs robustly: drop surrounding angle brackets/whitespace and
    casefold. Message-IDs are effectively unique tokens, so this is safe."""
    return value.strip().strip("<>").strip().casefold()


def _extract_state_headers(headers: list[dict]) -> dict[str, str]:
    """Keep only the STATE_HEADERS set, normalised to canonical casing. Ignores every
    other header and never touches a body part."""
    wanted = {h.lower(): h for h in STATE_HEADERS}
    out: dict[str, str] = {}
    for h in headers or []:
        name = h.get("name", "")
        canon = wanted.get(name.lower())
        if canon is not None:
            out[canon] = h.get("value", "")
    return out


# --- Credentials (resolved only at this execution boundary) -----------------

def find_client_secret_file(client_secret_dir: Optional[str] = None):
    """Discover the OAuth client-secret JSON by glob in the configured dir. The filename
    Google issues varies, so it is never hardcoded; the secret is never copied into the
    repo. Raises if absent."""
    from pathlib import Path

    d = Path(client_secret_dir or settings.gmail_client_secret_dir)
    matches = sorted(d.glob("client_secret_*.json"))
    if not matches:
        raise FileNotFoundError(
            f"No client_secret_*.json found in {d}. Download the Desktop-app OAuth "
            f"client credential there, or set JARVIS_GMAIL_CLIENT_SECRET_DIR."
        )
    return matches[0]


def load_gmail_credentials(
    vault: Optional[CredentialVault] = None,
    client_secret_dir: Optional[str] = None,
):
    """Return metadata-scoped Google credentials, loading/refreshing the vault token or
    running the installed-app consent flow on first use. The token is written back to the
    vault; it is never returned to or logged by the agent path (callers get a built
    service, never these credentials)."""
    vault = vault or CredentialVault()
    handle = vault.handle(GMAIL_TOKEN_KEY)

    creds = _load_token(vault, handle)
    if creds is not None and creds.valid:
        return creds
    if creds is not None and creds.expired and creds.refresh_token:
        creds = _refresh(creds)
        _store_token(vault, handle, creds)
        log.info("gmail_token_refreshed", scope="gmail.metadata")
        return creds

    secret_file = find_client_secret_file(client_secret_dir)
    creds = _run_installed_app_flow(str(secret_file), SCOPES)
    _store_token(vault, handle, creds)
    log.info("gmail_consent_completed", scope="gmail.metadata")
    return creds


def build_gmail_service(
    vault: Optional[CredentialVault] = None,
    client_secret_dir: Optional[str] = None,
) -> Any:
    """Build the Gmail API client from metadata-scoped vault credentials."""
    from googleapiclient.discovery import build

    creds = load_gmail_credentials(vault=vault, client_secret_dir=client_secret_dir)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# The google-specific seams below import lazily, so MessageState / confirm_message_state
# and their tests run without the google libraries installed (mirrors lazy Playwright).

def _load_token(vault: CredentialVault, handle: SecretHandle):
    raw = vault.read_bytes(handle)
    if raw is None:
        return None
    import json

    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)


def _store_token(vault: CredentialVault, handle: SecretHandle, creds) -> None:
    # to_json() includes the refresh token; it goes straight into the vault, never logged.
    vault.write_bytes(handle, creds.to_json().encode("utf-8"))


def _refresh(creds):
    from google.auth.transport.requests import Request

    creds.refresh(Request())
    return creds


def _run_installed_app_flow(client_secret_file: str, scopes: list[str]):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes)
    return flow.run_local_server(port=0)


# --- CLI --------------------------------------------------------------------

def _render(rfc_message_id: str, state: MessageState) -> str:
    lines = [
        "Gmail message state",
        f"  rfc message-id: {rfc_message_id}",
        f"  exists:         {state.exists}",
    ]
    if state.exists:
        lines += [
            f"  gmail id:       {state.gmail_id}",
            f"  labels:         {', '.join(state.labels) or '(none)'}",
            f"  is_sent:        {state.is_sent}",
            "  headers:",
        ]
        for k in STATE_HEADERS:
            if k in state.headers:
                lines.append(f"    {k}: {state.headers[k]}")
    return "\n".join(lines)


async def _amain(argv: list[str] | None = None) -> None:
    import argparse

    from ..config import configure_logging

    parser = argparse.ArgumentParser(prog="jarvis.providers.gmail_state")
    parser.add_argument(
        "--rfc-id",
        required=True,
        help='RFC 2822 Message-ID of a sent message, e.g. "<CAKs...@mail.gmail.com>"',
    )
    parser.add_argument(
        "--max-scan",
        type=int,
        default=DEFAULT_MAX_SCAN,
        help=f"max messages examined during Message-ID resolution (default {DEFAULT_MAX_SCAN})",
    )
    args = parser.parse_args(argv)

    configure_logging()
    state = await confirm_message_state(args.rfc_id, max_scan=args.max_scan)
    print(_render(args.rfc_id, state))


if __name__ == "__main__":
    asyncio.run(_amain())
