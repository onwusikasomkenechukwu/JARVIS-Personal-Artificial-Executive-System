"""Phase 2 — user-instructed email send tests.

No live API: a shared fake Gmail backend models one mailbox, exposed through TWO services
with separated capabilities — a send-scoped service that can only append (send) and a
metadata-scoped service that can only read (list/get). That mirrors the two-token
decorrelation: same underlying Gmail, different authority. The `X-Jarvis-Content-Hash`
header round-trips through the raw MIME exactly as in production (Gmail preserves it; it
rewrites the Message-ID, so the guard and verifier key on the content-hash).

The load-bearing test is `test_retry_after_timeout_success_sends_once`: a send that
succeeded server-side but timed out client-side must NOT be re-sent.
"""
import base64
import email
import inspect

import pytest

from jarvis.actions import send_email as se
from jarvis.actions.send_email import (
    CONTENT_HASH_HEADER,
    GMAIL_SEND_TOKEN_KEY,
    SEND_SCOPES,
    ConfirmationRecord,
    SendRequest,
    send_email,
    verify_sent,
)
from jarvis.providers import gmail_state
from jarvis.vault import CredentialVault


# --- A shared fake mailbox + two capability-separated services --------------

class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeBackend:
    """One mailbox. `append_from_raw` models Gmail accepting a send and preserving the
    client Message-ID + our custom content-hash header (it stores the raw)."""

    def __init__(self):
        self.sent = []  # oldest first

    def append_from_raw(self, raw_b64):
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw_b64))
        return self._add(msg["Message-ID"], msg[CONTENT_HASH_HEADER],
                         to=msg["To"], subject=msg["Subject"] or "")

    def _add(self, message_id, content_hash, to="me@example.com", subject="s"):
        rec = {
            "id": f"gid{len(self.sent) + 1}",
            "labelIds": ["SENT", "INBOX"],
            "headers": {
                "Message-Id": message_id,
                "To": to,
                "Subject": subject,
                "From": "me@example.com",
                "Date": "Wed, 17 Jun 2026 09:00:00 -0000",
                CONTENT_HASH_HEADER: content_hash,
            },
        }
        self.sent.append(rec)
        return rec["id"]


class FakeMetadataService:
    """gmail.metadata: list + get only. `send` is structurally unavailable (raises) —
    the metadata token cannot forge a send."""

    def __init__(self, backend):
        self.backend = backend
        self.calls = {"list": [], "get": []}

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, maxResults=100, pageToken=None, **extra):
        assert "q" not in extra and "query" not in extra, "metadata scope forbids q"
        self.calls["list"].append({"maxResults": maxResults, "pageToken": pageToken, **extra})
        ordered = list(reversed(self.backend.sent))  # newest first, like Gmail
        start = int(pageToken or 0)
        page = ordered[start:start + maxResults]
        resp = {"messages": [{"id": m["id"]} for m in page]}
        nxt = start + maxResults
        if nxt < len(ordered):
            resp["nextPageToken"] = str(nxt)
        return _Exec(resp)

    def get(self, userId, id, format, metadataHeaders=None, **extra):
        assert format == "metadata", "must never pull full/raw"
        self.calls["get"].append({"id": id, "format": format, "metadataHeaders": metadataHeaders})
        m = next((x for x in self.backend.sent if x["id"] == id), None)
        if m is None:
            return _Exec({})
        wanted = {h.lower() for h in (metadataHeaders or [])}
        headers = [{"name": n, "value": v} for n, v in m["headers"].items()
                   if not wanted or n.lower() in wanted]
        return _Exec({"id": m["id"], "labelIds": list(m["labelIds"]),
                      "payload": {"headers": headers}})

    def send(self, *a, **k):
        raise PermissionError("metadata scope cannot send")


class _SendExec:
    def __init__(self, svc, body):
        self.svc = svc
        self.body = body

    def execute(self):
        self.svc.send_calls += 1
        # Server-side ALWAYS records (the message reaches Gmail)...
        gid = self.svc.backend.append_from_raw(self.body["raw"])
        if self.svc.double_write:
            self.svc.backend.append_from_raw(self.body["raw"])  # provider double-wrote
        # ...but the first `fail_times` calls look like a CLIENT-SIDE timeout to us.
        if self.svc.send_calls <= self.svc.fail_times:
            raise TimeoutError("server accepted, client-side timeout")
        return {"id": gid, "labelIds": ["SENT"]}


class FakeSendService:
    """gmail.send: send only. list/get raise — the send token cannot read state, so it
    cannot confirm (or fake) its own delivery."""

    def __init__(self, backend, fail_times=0, double_write=False):
        self.backend = backend
        self.fail_times = fail_times
        self.double_write = double_write
        self.send_calls = 0

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        return _SendExec(self, body)

    def list(self, *a, **k):
        raise PermissionError("send scope cannot read state")

    def get(self, *a, **k):
        raise PermissionError("send scope cannot read state")


# --- Confirmation channels (out-of-band stand-ins) --------------------------

class CapturingChannel:
    """Records the prompt it was shown (to assert the full diff) and returns a fixed
    decision. Stands in for the separate-process approver."""

    def __init__(self, approve: bool):
        self.approve = approve
        self.prompt = None

    async def request(self, prompt):
        self.prompt = prompt
        return self.approve


def _req(body="hello from the phase-2 send test"):
    return SendRequest(to="me@example.com", subject="phase 2 send", body=body)


FAST = dict(send_backoff_s=0, verify_attempts=1, verify_delay_s=0)


# --- Tests ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_sends_and_verifies_once():
    backend = FakeBackend()
    send_svc = FakeSendService(backend)
    state_svc = FakeMetadataService(backend)
    ch = CapturingChannel(approve=True)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=ch, **FAST)

    assert r.sent is True
    assert r.verified is True
    assert r.already_sent is False
    assert r.duplicate_count == 1
    assert send_svc.send_calls == 1
    assert len(backend.sent) == 1
    assert state_svc.calls["get"], "verify/guard must use the metadata service"


@pytest.mark.asyncio
async def test_retry_after_timeout_success_sends_once():
    """THE load-bearing property. First attempt succeeds server-side but raises a
    client-side timeout; the retry's pre-send guard must find the SENT message and refuse,
    so exactly one email exists and send() is called exactly once."""
    backend = FakeBackend()
    send_svc = FakeSendService(backend, fail_times=1)  # 1st send: record then TimeoutError
    state_svc = FakeMetadataService(backend)
    ch = CapturingChannel(approve=True)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=ch, max_send_attempts=3, **FAST)

    assert send_svc.send_calls == 1, "the retry must NOT call send again"
    assert len(backend.sent) == 1, "exactly one email may exist"
    assert r.sent is True
    assert r.verified is True
    assert r.duplicate_count == 1
    assert "at-most-once" in r.note


@pytest.mark.asyncio
async def test_legitimate_retry_after_real_failure_still_sends():
    """Contrast: if the first attempt truly failed (nothing reached Gmail), the guard
    finds nothing and the retry legitimately sends. (Models a transient error that did
    NOT record server-side.)"""
    backend = FakeBackend()

    class FailFirstNoRecord(FakeSendService):
        def send(self, userId, body):
            outer = self

            class E:
                def execute(self_inner):
                    outer.send_calls += 1
                    if outer.send_calls == 1:
                        raise TimeoutError("no server-side record")
                    return {"id": outer.backend.append_from_raw(body["raw"]), "labelIds": ["SENT"]}
            return E()

    send_svc = FailFirstNoRecord(backend)
    state_svc = FakeMetadataService(backend)
    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=CapturingChannel(approve=True), max_send_attempts=3, **FAST)

    assert send_svc.send_calls == 2  # retried and succeeded
    assert len(backend.sent) == 1
    assert r.sent is True and r.duplicate_count == 1


@pytest.mark.asyncio
async def test_confirmation_denied_does_not_send():
    backend = FakeBackend()
    send_svc = FakeSendService(backend)
    state_svc = FakeMetadataService(backend)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=CapturingChannel(approve=False), **FAST)

    assert r.sent is False
    assert send_svc.send_calls == 0
    assert backend.sent == []
    assert r.confirmation is not None and r.confirmation.approved is False


@pytest.mark.asyncio
async def test_confirmation_timeout_is_default_safe_no_send(tmp_path):
    """Real FileConfirmationChannel with a short timeout and no approver: gate must resolve
    to deny, and nothing sends."""
    from jarvis.confirm import FileConfirmationChannel

    backend = FakeBackend()
    send_svc = FakeSendService(backend)
    state_svc = FakeMetadataService(backend)
    ch = FileConfirmationChannel(pending_dir=str(tmp_path / "pending"), timeout_s=0.3, poll_s=0.05)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=ch, **FAST)

    assert r.sent is False
    assert send_svc.send_calls == 0
    assert backend.sent == []
    assert "default-safe" in r.note


@pytest.mark.asyncio
async def test_confirmation_shows_full_body_untruncated():
    backend = FakeBackend()
    long_body = "LINE-ONE unique-marker-7f3\n" + ("x" * 500) + "\nLINE-LAST unique-marker-9c1"
    ch = CapturingChannel(approve=False)  # deny; we only care that the prompt was complete

    await send_email(SendRequest(to="me@example.com", subject="full body", body=long_body),
                     send_service=FakeSendService(backend),
                     state_service=FakeMetadataService(backend), channel=ch, **FAST)

    assert ch.prompt is not None
    assert long_body in ch.prompt.diff, "the full body must appear in the confirmation diff"
    assert "unique-marker-7f3" in ch.prompt.diff
    assert "unique-marker-9c1" in ch.prompt.diff  # the tail is present → not truncated


@pytest.mark.asyncio
async def test_two_scopes_are_capability_separated():
    backend = FakeBackend()
    send_svc = FakeSendService(backend)
    state_svc = FakeMetadataService(backend)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=CapturingChannel(approve=True), **FAST)
    assert r.sent is True

    # The send path used the send service; the guard/verify used the metadata service.
    assert send_svc.send_calls == 1
    assert state_svc.calls["list"] and state_svc.calls["get"]

    # Neither credential can do the other's job.
    with pytest.raises(PermissionError):
        state_svc.users().messages().send(userId="me", body={"raw": "x"})
    with pytest.raises(PermissionError):
        send_svc.users().messages().get(userId="me", id="gid1", format="metadata")


def test_send_and_metadata_use_distinct_scopes_and_tokens(tmp_path, monkeypatch):
    """The two credentials request different scopes and persist to different vault files;
    a future single-broad-scope or shared-token regression breaks here."""
    secret = tmp_path / "client_secret_x.apps.googleusercontent.com.json"
    secret.write_text("{}", encoding="utf-8")

    captured = []

    def fake_flow(client_secret_file, scopes):
        captured.append(list(scopes))
        from unittest.mock import MagicMock
        return MagicMock(to_json=lambda: '{"token": "x"}')

    monkeypatch.setattr(gmail_state, "_run_installed_app_flow", fake_flow)
    # don't actually build a real googleapiclient service (build is imported lazily)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())

    vault = CredentialVault(path=str(tmp_path / "vault"))

    se.build_gmail_send_service(vault=vault, client_secret_dir=str(tmp_path))
    gmail_state.build_gmail_service(vault=vault, client_secret_dir=str(tmp_path))

    assert SEND_SCOPES == ["https://www.googleapis.com/auth/gmail.send"]
    assert captured[0] == ["https://www.googleapis.com/auth/gmail.send"]      # send
    assert captured[1] == ["https://www.googleapis.com/auth/gmail.metadata"]  # metadata
    # Two separate token files in the vault.
    assert (tmp_path / "vault" / GMAIL_SEND_TOKEN_KEY).exists()
    assert (tmp_path / "vault" / gmail_state.GMAIL_TOKEN_KEY).exists()
    assert GMAIL_SEND_TOKEN_KEY != gmail_state.GMAIL_TOKEN_KEY


@pytest.mark.asyncio
async def test_double_send_detected_at_verify():
    # verify_sent counts SENT messages bearing the content-hash; >1 is the backstop signal.
    # (Gmail rewrites Message-IDs, so the content-hash header is the stable handle.)
    backend = FakeBackend()
    backend._add("<a@mail.gmail.com>", "hash-abc")
    backend._add("<b@mail.gmail.com>", "hash-abc")  # same content-hash ⇒ a double-send
    state_svc = FakeMetadataService(backend)

    outcome = await verify_sent(state_svc, "hash-abc", verify_scan=25)
    assert outcome.duplicate_count == 2
    assert outcome.verified is True


@pytest.mark.asyncio
async def test_pipeline_reports_double_send_loudly():
    """If the provider double-writes despite the guard, verify detects it and the result
    says so loudly."""
    backend = FakeBackend()
    send_svc = FakeSendService(backend, double_write=True)  # one send → two messages
    state_svc = FakeMetadataService(backend)

    r = await send_email(_req(), send_service=send_svc, state_service=state_svc,
                         channel=CapturingChannel(approve=True), **FAST)

    assert r.duplicate_count == 2
    assert "DOUBLE-SEND DETECTED" in r.note


@pytest.mark.asyncio
async def test_already_sent_before_confirm_refuses_without_asking():
    backend = FakeBackend()
    req = _req()
    backend._add("<prior@mail.gmail.com>", req.content_hash)  # a prior identical send exists
    send_svc = FakeSendService(backend)
    state_svc = FakeMetadataService(backend)
    ch = CapturingChannel(approve=True)

    r = await send_email(req, send_service=send_svc, state_service=state_svc, channel=ch, **FAST)

    assert r.already_sent is True
    assert send_svc.send_calls == 0
    assert ch.prompt is None, "must not even prompt the user when already sent"
    assert len(backend.sent) == 1


# --- Provenance / no-untrusted-path guarantees ------------------------------

def test_send_request_has_no_provenance_field_and_cannot_carry_untrusted():
    fields = set(SendRequest.model_fields)
    assert fields == {"to", "subject", "body"}
    assert "provenance" not in fields  # provenance is not user-supplied; it is fixed in code


def test_send_email_has_no_provenance_parameter():
    params = set(inspect.signature(send_email).parameters)
    assert "provenance" not in params  # no way to inject a non-USER_DIRECT provenance


@pytest.mark.asyncio
async def test_send_is_tagged_user_direct():
    backend = FakeBackend()
    r = await send_email(_req(), send_service=FakeSendService(backend),
                         state_service=FakeMetadataService(backend),
                         channel=CapturingChannel(approve=True), **FAST)
    assert isinstance(r.confirmation, ConfirmationRecord)
    assert r.confirmation.provenance == "USER_DIRECT"
    assert r.confirmation.source == "user"


def test_content_hash_is_stable_and_normalised():
    a = SendRequest(to="Me@Example.com ", subject=" hi ", body="b")
    b = SendRequest(to="me@example.com", subject="hi", body="b")
    assert a.content_hash == b.content_hash  # recipient case/trim + subject trim normalised
    c = SendRequest(to="me@example.com", subject="hi", body="different")
    assert c.content_hash != b.content_hash
