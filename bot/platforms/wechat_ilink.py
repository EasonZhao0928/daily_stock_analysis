"""Personal WeChat channel adapter over an injectable iLink-like protocol.

The real endpoint is deliberately configured rather than hard-coded to a
provider account.  Tests use ``FakeILinkServer`` and never contact WeChat.
The adapter is feature-flagged off by default and only sends text replies.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence
from urllib import request as urllib_request

from bot.conversation import InboundEnvelope
from bot.models import BotResponse, ChatType

logger = logging.getLogger(__name__)


class JsonTransport(Protocol):
    def post(self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Mapping[str, Any]: ...


class UrlJsonTransport:
    """Minimal standard-library JSON transport for a configured base URL."""

    def __init__(self, base_url: str, timeout_seconds: float = 20.0):
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def post(self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Mapping[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            f"{self.base_url}/{str(path).lstrip('/')}",
            data=body,
            headers={"Content-Type": "application/json", **dict(headers)},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310 - user-configured endpoint
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("iLink response must be an object")
        return data


class WeChatILinkClient:
    """Protocol client with no persistence of access tokens or context tokens."""

    def __init__(self, *, base_url: str, token: str, transport: Optional[JsonTransport] = None):
        if not str(base_url).strip():
            raise ValueError("WeChat iLink base URL is required")
        if not str(token).strip():
            raise ValueError("WeChat iLink token is required")
        self._token = str(token)
        self._transport = transport or UrlJsonTransport(base_url)

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        # Keep token in an in-memory header only; never put it in logs/raw data.
        return self._transport.post(path, payload, {"Authorization": f"Bearer {self._token}"})

    def get_qrcode(self) -> Mapping[str, Any]:
        return self._post("/ilink/bot/get_qrcode", {})

    def get_qrcode_status(self, qrcode: str) -> Mapping[str, Any]:
        return self._post("/ilink/bot/get_qrcode_status", {"qrcode": str(qrcode)})

    def get_updates(self, cursor: str = "", *, timeout_ms: int = 30000) -> Mapping[str, Any]:
        return self._post("/ilink/bot/get_updates", {"cursor": str(cursor), "timeout_ms": int(timeout_ms)})

    def send_text(self, *, user_id: str, text: str, context_token: Optional[str] = None) -> Mapping[str, Any]:
        payload: Dict[str, Any] = {
            "msg": {
                "to_user_id": str(user_id),
                "client_id": uuid.uuid4().hex,
                "item_list": [{"type": "text", "text": str(text)}],
            }
        }
        if context_token:
            payload["msg"]["context_token"] = str(context_token)
        return self._post("/ilink/bot/send_message", payload)


@dataclass
class WeChatPairingState:
    """In-memory pairing state; QR/token material is never persisted."""

    status: str = "signed_out"
    qrcode: Optional[str] = None
    expires_at: Optional[float] = None
    bound_user_id: Optional[str] = None
    last_error: Optional[str] = None


class WeChatPairingController:
    """Explicit QR pairing/login-loss state machine for personal use."""

    def __init__(self, client: WeChatILinkClient, *, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.clock = clock
        self.state = WeChatPairingState()

    def begin(self) -> WeChatPairingState:
        payload = self.client.get_qrcode()
        qr = str(payload.get("qrcode") or payload.get("qr_code") or "")
        if not qr:
            raise RuntimeError("iLink pairing response did not include a QR code")
        expires = payload.get("expires_in") or payload.get("expiresIn") or 300
        self.state = WeChatPairingState(
            status="pending",
            qrcode=qr,
            expires_at=self.clock() + max(1, int(expires)),
        )
        return self.state

    def poll(self) -> WeChatPairingState:
        if self.state.status != "pending" or not self.state.qrcode:
            return self.state
        if self.state.expires_at is not None and self.clock() >= self.state.expires_at:
            self.state.status = "expired"
            return self.state
        payload = self.client.get_qrcode_status(self.state.qrcode)
        status = str(payload.get("status") or payload.get("state") or "wait").lower()
        if status in {"confirmed", "confirmed_login", "logged_in", "ok"}:
            self.state.status = "paired"
            self.state.bound_user_id = str(payload.get("user_id") or payload.get("userId") or "") or None
        elif status in {"expired", "timeout"}:
            self.state.status = "expired"
        elif status in {"unauthorized", "login_loss", "logged_out"}:
            self.state.status = "login_lost"
        return self.state

    def logout(self) -> WeChatPairingState:
        self.state = WeChatPairingState(status="signed_out")
        return self.state


def _items(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    values = payload.get("messages") or payload.get("items") or payload.get("msgs") or []
    return [value for value in values if isinstance(value, Mapping)] if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) else []


def parse_ilink_messages(payload: Mapping[str, Any]) -> list[InboundEnvelope]:
    """Normalize common iLink/fake-server text message shapes."""
    envelopes: list[InboundEnvelope] = []
    for item in _items(payload):
        msg_type = str(item.get("type") or item.get("msg_type") or "text").lower()
        if msg_type not in {"text", "1"}:
            continue
        user_id = str(item.get("from_user_id") or item.get("fromUserId") or item.get("user_id") or "")
        conversation_id = str(item.get("conversation_id") or item.get("conversationId") or user_id)
        message_id = str(item.get("message_id") or item.get("msg_id") or item.get("id") or "")
        text = str(item.get("text") or item.get("content") or "").strip()
        if not user_id or not message_id or not text:
            continue
        envelopes.append(InboundEnvelope(
            channel="wechat",
            message_id=message_id,
            user_id=user_id,
            user_name=str(item.get("user_name") or item.get("nickname") or user_id),
            conversation_id=conversation_id,
            text=text,
            chat_type=ChatType.PRIVATE,
            reply_context={"context_token": item.get("context_token") or item.get("contextToken")},
            raw={"type": msg_type, "message_id": message_id},
        ))
    return envelopes


@dataclass
class WeChatChannelHealth:
    enabled: bool
    running: bool
    cursor: str
    received: int = 0
    sent: int = 0
    failures: int = 0
    last_error: Optional[str] = None
    auth_state: str = "unknown"
    pairing_status: str = "signed_out"


class WeChatChannelAdapter:
    """Long-poll adapter.  It is inert unless ``enabled=True`` is explicit."""

    channel_name = "wechat"

    def __init__(
        self,
        client: WeChatILinkClient,
        *,
        enabled: bool = False,
        poll_timeout_ms: int = 30000,
        idle_sleep_seconds: float = 0.2,
    ):
        self.client = client
        self.enabled = bool(enabled)
        self.poll_timeout_ms = max(1000, int(poll_timeout_ms))
        self.idle_sleep_seconds = max(0.01, float(idle_sleep_seconds))
        self._cursor = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_message: Optional[Callable[[InboundEnvelope], bool]] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._received = 0
        self._sent = 0
        self._failures = 0
        self._last_error: Optional[str] = None
        self._failure_streak = 0
        self._auth_state = "unknown"
        self._pairing = WeChatPairingState(status="signed_out")
        self._last_cursor_key: tuple[int, str] = (0, "")

    @staticmethod
    def _cursor_key(value: Any) -> tuple[int, str]:
        """Order cursors so the initial empty cursor is always the floor.

        The ranks previously put numeric cursors (rank 0) *below* the empty
        starting cursor (rank 1), so ``next >= current`` was never true and the
        cursor never advanced: every poll replayed the whole backlog.
        """
        text = str(value or "").strip()
        if not text:
            return (0, "")
        try:
            return (1, f"{int(text):020d}")
        except (TypeError, ValueError):
            return (2, text)

    def start(self, on_message: Callable[[InboundEnvelope], bool]) -> None:
        if not self.enabled:
            raise RuntimeError("WeChat channel is disabled; set the feature flag explicitly")
        with self._lock:
            if self._running:
                return
            self._on_message = on_message
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="wechat-ilink")
            self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self.client.get_updates(self._cursor, timeout_ms=self.poll_timeout_ms)
                status = str(payload.get("status") or payload.get("error") or "").lower()
                if status in {"unauthorized", "token_expired", "login_lost", "logged_out"} or payload.get("code") in {401, 403}:
                    self._auth_state = "login_lost"
                    self._pairing.status = "login_lost"
                    raise PermissionError("wechat iLink login lost or token expired")
                next_cursor = payload.get("cursor") or payload.get("next_cursor")
                if next_cursor is not None and self._cursor_key(next_cursor) >= self._cursor_key(self._cursor):
                    self._cursor = str(next_cursor)
                messages = parse_ilink_messages(payload)
                self._failure_streak = 0
                self._auth_state = "authenticated"
                self._received += len(messages)
                for envelope in messages:
                    if self._on_message is not None:
                        self._on_message(envelope)
                if not messages:
                    self._stop.wait(self.idle_sleep_seconds)
            except Exception as exc:
                self._failures += 1
                self._failure_streak += 1
                self._last_error = str(exc)
                if isinstance(exc, PermissionError):
                    self._auth_state = "login_lost"
                logger.warning("WeChat iLink poll failed: %s", type(exc).__name__)
                backoff = min(10.0, max(self.idle_sleep_seconds, 0.2) * (2 ** min(self._failure_streak - 1, 5)))
                self._stop.wait(backoff)
        with self._lock:
            self._running = False

    def send(self, envelope: InboundEnvelope, response: BotResponse) -> bool:
        try:
            context_token = envelope.reply_context.get("context_token") if envelope.reply_context else None
            self.client.send_text(user_id=envelope.user_id, text=response.text, context_token=context_token)
            self._sent += 1
            return True
        except Exception as exc:
            self._failures += 1
            self._last_error = str(exc)
            logger.warning("WeChat iLink send failed: %s", type(exc).__name__)
            return False

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._running = False

    def health(self) -> WeChatChannelHealth:
        return WeChatChannelHealth(
            enabled=self.enabled,
            running=self._running,
            cursor=self._cursor,
            received=self._received,
            sent=self._sent,
            failures=self._failures,
            last_error=self._last_error,
            auth_state=self._auth_state,
            pairing_status=self._pairing.status,
        )


@dataclass
class FakeILinkServer:
    """Deterministic fake protocol server for lifecycle and failure tests."""

    updates: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    qr_status: str = "wait"
    cursor: str = ""
    fail_next: Optional[str] = None
    expected_token: Optional[str] = None
    token_expired: bool = False
    out_of_order_cursor: Optional[str] = None

    def post(self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Mapping[str, Any]:
        if self.expected_token is not None and headers.get("Authorization") != f"Bearer {self.expected_token}":
            raise PermissionError("unauthorized")
        if self.token_expired:
            return {"status": "token_expired", "code": 401}
        if self.fail_next:
            failure = self.fail_next
            self.fail_next = None
            raise RuntimeError(failure)
        if path.endswith("get_qrcode"):
            return {"qrcode": "fake-qrcode", "message": "scan"}
        if path.endswith("get_qrcode_status"):
            return {"status": self.qr_status}
        if path.endswith("get_updates"):
            messages = list(self.updates)
            self.updates.clear()
            cursor = self.out_of_order_cursor if self.out_of_order_cursor is not None else self.cursor
            return {"cursor": cursor, "messages": messages}
        if path.endswith("send_message"):
            self.sent.append(dict(payload))
            return {"ret": 0}
        raise RuntimeError(f"unknown fake iLink path: {path}")


__all__ = [
    "FakeILinkServer",
    "UrlJsonTransport",
    "WeChatChannelAdapter",
    "WeChatChannelHealth",
    "WeChatILinkClient",
    "WeChatPairingController",
    "WeChatPairingState",
    "parse_ilink_messages",
]
