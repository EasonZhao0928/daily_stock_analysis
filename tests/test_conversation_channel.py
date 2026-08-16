"""Deterministic lifecycle and personal WeChat channel contracts."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

from bot.conversation import ConversationChannelRuntime, InboundEnvelope, sanitize_outbound_response
from bot.credentials import FileCredentialStore
from bot.models import BotResponse, ChatType
from bot.platforms.wechat_ilink import FakeILinkServer, WeChatChannelAdapter, WeChatILinkClient, WeChatPairingController, parse_ilink_messages


class _Adapter:
    channel_name = "fake"

    def __init__(self):
        self.callback = None
        self.sent = []
        self.started = False

    def start(self, on_message):
        self.started = True
        self.callback = on_message

    def stop(self):
        self.started = False

    def send(self, envelope, response):
        self.sent.append((envelope.message_id, response.text))
        return True


def _envelope(message_id: str, text: str, user_id: str = "owner") -> InboundEnvelope:
    return InboundEnvelope(
        channel="fake",
        message_id=message_id,
        user_id=user_id,
        user_name=user_id,
        conversation_id="conversation",
        text=text,
        chat_type=ChatType.PRIVATE,
    )


def test_runtime_lifecycle_allowlist_dedupe_and_fifo():
    seen = []
    done = threading.Event()
    adapter = _Adapter()

    def handle(message):
        seen.append(message.content)
        if len(seen) == 2:
            done.set()
        return BotResponse.text_response(f"ok:{message.content}", at_user=False)

    runtime = ConversationChannelRuntime(channel="fake", on_message=handle, allowlist={"owner"}, max_workers=2)
    runtime.start(adapter)
    assert adapter.started
    assert adapter.callback(_envelope("1", "first")) is True
    assert adapter.callback(_envelope("1", "first")) is False
    assert adapter.callback(_envelope("2", "second")) is True
    assert adapter.callback(_envelope("3", "blocked", user_id="other")) is False
    assert done.wait(2)
    runtime.stop(wait=True)

    assert seen == ["first", "second"]
    assert adapter.sent == [("1", "ok:first"), ("2", "ok:second")]
    health = runtime.health()
    assert health.running is False
    assert health.accepted == 2
    assert health.duplicate == 1
    assert health.rejected == 1


def test_wechat_parse_and_fake_ilink_poll_roundtrip():
    payload = {"messages": [{"msg_id": "m1", "from_user_id": "wx-owner", "conversation_id": "wx-owner", "type": "text", "text": "/help", "context_token": "opaque"}, {"msg_id": "m2", "from_user_id": "wx-owner", "type": "image"}]}
    messages = parse_ilink_messages(payload)
    assert len(messages) == 1
    assert messages[0].reply_context["context_token"] == "opaque"

    server = FakeILinkServer(updates=[{"msg_id": "m3", "from_user_id": "wx-owner", "type": "text", "text": "/status", "context_token": "ctx"}])
    client = WeChatILinkClient(base_url="https://fake.invalid", token="opaque-token", transport=server)
    adapter = WeChatChannelAdapter(client, enabled=True, poll_timeout_ms=1000, idle_sleep_seconds=0.01)
    received = threading.Event()
    runtime = ConversationChannelRuntime(
        channel="wechat",
        on_message=lambda message: (received.set() or BotResponse.text_response("safe reply", at_user=False)),
        allowlist={"wx-owner"},
    )
    runtime.start(adapter)
    assert received.wait(2)
    time.sleep(0.05)
    runtime.stop(wait=True)
    assert server.sent and server.sent[0]["msg"]["context_token"] == "ctx"
    assert adapter.health().received >= 1


def test_file_credential_store_is_0600_and_does_not_log_or_store_in_db(tmp_path: Path):
    path = tmp_path / "credentials.json"
    store = FileCredentialStore(path)
    store.write("wechat-ref", "secret-value")
    assert store.read("wechat-ref") == "secret-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.delete("wechat-ref")
    assert store.read("wechat-ref") is None


def test_runtime_redacts_sensitive_outbound_fragments_and_empty_allowlist_denies():
    adapter = _Adapter()
    sent = threading.Event()

    def handle(_message):
        sent.set()
        return BotResponse.text_response("token=do-not-send secret: still-private", at_user=False)

    runtime = ConversationChannelRuntime(channel="fake", on_message=handle, allowlist=set())
    runtime.start(adapter)
    assert runtime.receive(_envelope("blocked", "hello", user_id="owner")) is False
    runtime.stop(wait=True)

    safe = sanitize_outbound_response(BotResponse.text_response("Authorization: Bearer abc123"))
    assert "abc123" not in safe.text
    assert "REDACTED" in safe.text


def test_fake_ilink_rejects_wrong_token_and_exposes_qr_login_state():
    server = FakeILinkServer(expected_token="right", qr_status="expired")
    bad_client = WeChatILinkClient(base_url="https://fake.invalid", token="wrong", transport=server)
    try:
        bad_client.get_updates()
    except PermissionError:
        pass
    else:
        raise AssertionError("wrong token must be rejected")

    client = WeChatILinkClient(base_url="https://fake.invalid", token="right", transport=server)
    assert client.get_qrcode_status("fake")['status'] == "expired"


def test_wechat_pairing_expiry_login_loss_and_cursor_regression() -> None:
    now = [100.0]
    server = FakeILinkServer(qr_status="wait", cursor="10")
    client = WeChatILinkClient(base_url="https://fake.invalid", token="right", transport=server)
    pairing = WeChatPairingController(client, clock=lambda: now[0])
    state = pairing.begin()
    assert state.status == "pending"
    now[0] = state.expires_at + 1
    assert pairing.poll().status == "expired"

    server = FakeILinkServer(token_expired=True, cursor="10")
    client = WeChatILinkClient(base_url="https://fake.invalid", token="right", transport=server)
    adapter = WeChatChannelAdapter(client, enabled=True, poll_timeout_ms=1000, idle_sleep_seconds=0.01)
    adapter.start(lambda _message: True)
    deadline = time.time() + 1
    while adapter.health().auth_state != "login_lost" and time.time() < deadline:
        time.sleep(0.01)
    adapter.stop()
    assert adapter.health().auth_state == "login_lost"

    class CursorRegressionServer(FakeILinkServer):
        calls: int = 0

        def post(self, path, payload, headers):
            if path.endswith("get_updates"):
                self.calls += 1
            return super().post(path, payload, headers)

    # N3 regression: starting from the real default (empty) cursor, the first
    # numeric cursor must be accepted.  The old ranking sorted every numeric
    # cursor below the empty sentinel, so the cursor never left "" and each
    # poll replayed the entire backlog.
    advancing = CursorRegressionServer(cursor="7")
    client = WeChatILinkClient(base_url="https://fake.invalid", token="right", transport=advancing)
    adapter = WeChatChannelAdapter(client, enabled=True, poll_timeout_ms=1000, idle_sleep_seconds=0.01)
    assert adapter.health().cursor == ""
    adapter.start(lambda _message: True)
    deadline = time.time() + 1
    while advancing.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    adapter.stop()
    assert adapter.health().cursor == "7", "cursor must advance off the empty default"

    server = CursorRegressionServer(cursor="10", out_of_order_cursor="9")
    client = WeChatILinkClient(base_url="https://fake.invalid", token="right", transport=server)
    adapter = WeChatChannelAdapter(client, enabled=True, poll_timeout_ms=1000, idle_sleep_seconds=0.01)
    adapter._cursor = "10"
    adapter.start(lambda _message: True)
    deadline = time.time() + 1
    while server.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    adapter.stop()
    assert server.calls >= 1
    assert adapter.health().cursor == "10"
