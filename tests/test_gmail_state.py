"""Phase 2 — Gmail provider state-read tests.

The live API is never touched: a fake service models Gmail's list/get under the metadata
scope. These pin the state/content boundary (scope + format + type) and the metadata-safe
resolution (no `q`), so a future widening or regression breaks a test.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarvis.providers import gmail_state
from jarvis.providers.gmail_state import (
    GMAIL_METADATA_SCOPE,
    MESSAGE_FORMAT,
    SCOPES,
    MessageState,
    confirm_message_state,
    find_client_secret_file,
)
from jarvis.vault import CredentialVault


# --- A fake Gmail service modelling metadata-scope list/get -----------------

class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    """Models users().messages(): list paginates ids; get returns labels + the requested
    metadata headers. Records every list/get kwargs so tests can assert on them."""

    def __init__(self, store, calls):
        self._store = store  # list of {"id","labelIds","headers"(dict),"snippet"(opt)}
        self.calls = calls

    def list(self, userId, maxResults=100, pageToken=None, **extra):
        self.calls["list"].append({"userId": userId, "maxResults": maxResults,
                                    "pageToken": pageToken, **extra})
        start = int(pageToken or 0)
        page = self._store[start:start + maxResults]
        resp = {"messages": [{"id": m["id"]} for m in page],
                "resultSizeEstimate": len(self._store)}
        nxt = start + maxResults
        if nxt < len(self._store):
            resp["nextPageToken"] = str(nxt)
        return _Exec(resp)

    def get(self, userId, id, format, metadataHeaders=None, **extra):
        self.calls["get"].append({"id": id, "format": format,
                                  "metadataHeaders": metadataHeaders, **extra})
        m = next((x for x in self._store if x["id"] == id), None)
        if m is None:
            return _Exec({})
        wanted = {h.lower() for h in (metadataHeaders or [])}
        headers = [{"name": n, "value": v} for n, v in m["headers"].items()
                   if not wanted or n.lower() in wanted]
        resp = {"id": m["id"], "labelIds": list(m["labelIds"]),
                "payload": {"headers": headers}}
        if "snippet" in m:
            resp["snippet"] = m["snippet"]
        return _Exec(resp)


class _FakeService:
    def __init__(self, store):
        self.calls = {"list": [], "get": []}
        self._messages = _FakeMessages(store, self.calls)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _sent_msg(mid="<abc@mail.gmail.com>"):
    return {
        "id": "18ab12cd34ef",
        "labelIds": ["SENT", "INBOX", "CATEGORY_PERSONAL"],
        "snippet": "this body-derived snippet must never be read into MessageState",
        "headers": {
            "Message-Id": mid,
            "From": "me@example.com",
            "To": "me@example.com",
            "Subject": "phase 2 test",
            "Date": "Wed, 17 Jun 2026 09:00:00 -0000",
            "Received": "should be ignored",
        },
    }


# --- Tests ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sent_message_reports_is_sent_true():
    svc = _FakeService([_sent_msg()])
    state = await confirm_message_state("<abc@mail.gmail.com>", service=svc)

    assert state.exists is True
    assert state.is_sent is True
    assert state.gmail_id == "18ab12cd34ef"
    assert "SENT" in state.labels
    assert state.headers == {
        "From": "me@example.com",
        "To": "me@example.com",
        "Subject": "phase 2 test",
        "Date": "Wed, 17 Jun 2026 09:00:00 -0000",
    }


@pytest.mark.asyncio
async def test_match_is_robust_to_angle_brackets_and_case():
    # stored with brackets + mixed case; caller passes a bare, differently-cased id
    svc = _FakeService([_sent_msg(mid="<AbC@Mail.Gmail.Com>")])
    state = await confirm_message_state("abc@mail.gmail.com", service=svc)
    assert state.exists is True and state.is_sent is True


@pytest.mark.asyncio
async def test_unresolvable_message_id_returns_exists_false():
    svc = _FakeService([_sent_msg(mid="<other@mail.gmail.com>")])
    state = await confirm_message_state("<nope@mail.gmail.com>", service=svc)

    assert state.exists is False
    assert state.gmail_id is None
    assert state.is_sent is False
    assert state.labels == []
    assert state.headers == {}


@pytest.mark.asyncio
async def test_resolution_never_uses_q_parameter():
    # The metadata scope rejects `q`; the resolver must never pass it.
    svc = _FakeService([_sent_msg(mid=f"<m{i}@mail.gmail.com>") | {"id": f"id{i}"}
                        for i in range(5)])
    await confirm_message_state("<m3@mail.gmail.com>", service=svc)
    for call in svc.calls["list"]:
        assert "q" not in call
        assert "query" not in call


@pytest.mark.asyncio
async def test_get_called_with_metadata_format_not_full_or_raw():
    svc = _FakeService([_sent_msg()])
    await confirm_message_state("<abc@mail.gmail.com>", service=svc)

    assert svc.calls["get"], "get was never called"
    for call in svc.calls["get"]:
        assert call["format"] == "metadata"
        assert call["format"] not in ("full", "raw")
    assert MESSAGE_FORMAT == "metadata"


@pytest.mark.asyncio
async def test_max_scan_bounds_the_search():
    # target is the 6th message but max_scan=3 -> not found, and at most 3 gets happen
    store = [_sent_msg(mid=f"<m{i}@mail.gmail.com>") | {"id": f"id{i}"} for i in range(10)]
    svc = _FakeService(store)
    state = await confirm_message_state("<m5@mail.gmail.com>", service=svc, max_scan=3)
    assert state.exists is False
    assert len(svc.calls["get"]) <= 3


@pytest.mark.asyncio
async def test_snippet_is_never_read_into_state():
    svc = _FakeService([_sent_msg()])
    state = await confirm_message_state("<abc@mail.gmail.com>", service=svc)
    assert "snippet" not in state.model_dump()
    for value in state.model_dump().values():
        assert "body-derived snippet" not in str(value)


def test_message_state_has_no_body_snippet_or_payload_field():
    fields = set(MessageState.model_fields)
    assert fields == {"exists", "gmail_id", "labels", "headers", "is_sent"}
    for forbidden in ("body", "snippet", "payload", "raw", "content", "parts"):
        assert forbidden not in fields


def test_scope_is_metadata_exactly():
    # A future widening to gmail.readonly (or anything broader) must break here.
    assert GMAIL_METADATA_SCOPE == "https://www.googleapis.com/auth/gmail.metadata"
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.metadata"]
    assert "readonly" not in GMAIL_METADATA_SCOPE
    assert "https://mail.google.com/" not in SCOPES


def test_consent_flow_requests_only_metadata_scope(tmp_path, monkeypatch):
    """The credential loader must hand exactly the metadata scope to the consent flow."""
    secret = tmp_path / "client_secret_123.apps.googleusercontent.com.json"
    secret.write_text("{}", encoding="utf-8")

    captured = {}

    def fake_flow(client_secret_file, scopes):
        captured["file"] = client_secret_file
        captured["scopes"] = scopes
        return MagicMock(to_json=lambda: '{"token": "x"}')

    monkeypatch.setattr(gmail_state, "_run_installed_app_flow", fake_flow)

    vault = CredentialVault(path=str(tmp_path / "vault"))
    gmail_state.load_gmail_credentials(vault=vault, client_secret_dir=str(tmp_path))

    assert captured["scopes"] == ["https://www.googleapis.com/auth/gmail.metadata"]
    assert captured["file"] == str(secret)


def test_client_secret_loaded_from_configured_dir_not_repo(tmp_path, monkeypatch):
    # The configured dir is the source of truth; discovery is by glob, never hardcoded.
    secret = tmp_path / "client_secret_987.apps.googleusercontent.com.json"
    secret.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("JARVIS_GMAIL_CLIENT_SECRET_DIR", str(tmp_path))
    found = find_client_secret_file(str(tmp_path))

    assert found == secret
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in found.resolve().parents


def test_missing_client_secret_raises():
    with pytest.raises(FileNotFoundError):
        find_client_secret_file(str(Path(__file__).parent / "no_such_dir"))


def test_vault_handle_does_not_expose_secret():
    vault = CredentialVault(path="/tmp/whatever")
    handle = vault.handle("gmail_metadata_token.json")
    assert "gmail_metadata_token.json" in repr(handle)
    assert "token" in repr(handle)  # the key, not a value
