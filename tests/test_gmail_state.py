"""Phase 2 — Gmail provider state-read tests.

The live API is never touched: a MagicMock stands in for the Gmail service. These pin
the state/content boundary (scope + format + type) so a future widening breaks a test.
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


def _service(list_result: dict, get_result: dict | None = None) -> MagicMock:
    """A MagicMock shaped like the Gmail client: users().messages().list/get().execute()."""
    svc = MagicMock()
    messages = svc.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = list_result
    if get_result is not None:
        messages.get.return_value.execute.return_value = get_result
    return svc


def _sent_get_result() -> dict:
    return {
        "id": "18ab12cd34ef",
        "labelIds": ["SENT", "INBOX", "CATEGORY_PERSONAL"],
        "snippet": "this body-derived snippet must never be read into MessageState",
        "payload": {
            "headers": [
                {"name": "From", "value": "me@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "phase 2 test"},
                {"name": "Date", "value": "Wed, 17 Jun 2026 09:00:00 -0000"},
                {"name": "Received", "value": "should be ignored"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_sent_message_reports_is_sent_true():
    svc = _service(
        list_result={"messages": [{"id": "18ab12cd34ef"}], "resultSizeEstimate": 1},
        get_result=_sent_get_result(),
    )
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
async def test_unresolvable_message_id_returns_exists_false():
    svc = _service(list_result={"resultSizeEstimate": 0})  # empty list -> no `messages`
    state = await confirm_message_state("<nope@mail.gmail.com>", service=svc)

    assert state.exists is False
    assert state.gmail_id is None
    assert state.is_sent is False
    assert state.labels == []
    assert state.headers == {}
    # never called get when nothing resolved
    svc.users.return_value.messages.return_value.get.assert_not_called()


@pytest.mark.asyncio
async def test_get_called_with_metadata_format_not_full_or_raw():
    svc = _service(
        list_result={"messages": [{"id": "18ab12cd34ef"}]},
        get_result=_sent_get_result(),
    )
    await confirm_message_state("<abc@mail.gmail.com>", service=svc)

    get = svc.users.return_value.messages.return_value.get
    get.assert_called_once()
    _, kwargs = get.call_args
    assert kwargs["format"] == "metadata"
    assert kwargs["format"] not in ("full", "raw")
    assert MESSAGE_FORMAT == "metadata"


@pytest.mark.asyncio
async def test_snippet_is_never_read_into_state():
    svc = _service(
        list_result={"messages": [{"id": "18ab12cd34ef"}]},
        get_result=_sent_get_result(),  # carries a "snippet"
    )
    state = await confirm_message_state("<abc@mail.gmail.com>", service=svc)
    # No field, anywhere, holds the snippet text.
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
    # never resolved from inside the repo working tree
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in found.resolve().parents


def test_missing_client_secret_raises():
    with pytest.raises(FileNotFoundError):
        find_client_secret_file(str(Path(__file__).parent / "no_such_dir"))


def test_vault_handle_does_not_expose_secret():
    vault = CredentialVault(path="/tmp/whatever")
    handle = vault.handle("gmail_metadata_token.json")
    # repr/str carry the key name, never any secret payload.
    assert "gmail_metadata_token.json" in repr(handle)
    assert "token" in repr(handle)  # the key, not a value
