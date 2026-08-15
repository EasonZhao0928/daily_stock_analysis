"""Provider-neutral conversation channel runtime.

The runtime owns lifecycle, deduplication, allowlist checks and per
conversation ordering.  Platform adapters only translate protocol payloads to
``InboundEnvelope`` and send a ``BotResponse`` back using the opaque reply
context; credentials and context tokens never enter business persistence.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Protocol, Set

from bot.models import BotMessage, BotResponse, ChatType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundEnvelope:
    """Normalized inbound message with an opaque platform reply context."""

    channel: str
    message_id: str
    user_id: str
    user_name: str
    conversation_id: str
    text: str
    # The platform chat id is kept separate from the ordering key.  Adapters
    # can use a composite conversation_id (for example group+user) without
    # leaking that implementation detail into BotMessage.chat_id.
    chat_id: str = ""
    chat_type: ChatType = ChatType.PRIVATE
    reply_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    timestamp: datetime = field(default_factory=datetime.now)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def dedupe_key(self) -> str:
        return f"{self.channel}:{self.message_id or self.conversation_id + ':' + self.text}"

    @property
    def conversation_key(self) -> str:
        return f"{self.channel}:{self.conversation_id or self.user_id}"

    def to_bot_message(self) -> BotMessage:
        return BotMessage(
            platform=self.channel,
            message_id=self.message_id,
            user_id=self.user_id,
            user_name=self.user_name,
            chat_id=self.chat_id or self.conversation_id,
            chat_type=self.chat_type,
            content=self.text,
            raw_content=self.text,
            mentioned=True,
            timestamp=self.timestamp,
            raw_data=dict(self.raw),
        )


def envelope_from_bot_message(
    message: BotMessage,
    *,
    conversation_id: Optional[str] = None,
    reply_context: Optional[Mapping[str, Any]] = None,
) -> InboundEnvelope:
    """Convert a legacy platform message into the provider-neutral envelope."""

    return InboundEnvelope(
        channel=str(message.platform),
        message_id=str(message.message_id or ""),
        user_id=str(message.user_id or ""),
        user_name=str(message.user_name or ""),
        conversation_id=str(conversation_id or message.chat_id or message.user_id or message.message_id),
        chat_id=str(message.chat_id or ""),
        text=str(message.content or ""),
        chat_type=message.chat_type,
        reply_context=dict(reply_context or {}),
        timestamp=message.timestamp,
        raw=dict(message.raw_data or {}),
    )


class ConversationChannelAdapter(Protocol):
    """Minimal adapter seam used by the runtime and fake protocol tests."""

    channel_name: str

    def start(self, on_message: Callable[[InboundEnvelope], bool]) -> None: ...

    def stop(self) -> None: ...

    def send(self, envelope: InboundEnvelope, response: BotResponse) -> bool: ...


class CallbackChannelAdapter:
    """Small bridge for existing SDK handlers.

    The platform-specific parser calls :meth:`emit`; the runtime owns queue,
    dedupe and worker lifecycle.  ``sender`` receives the original envelope
    and normalized response, keeping reply context in memory only.
    """

    def __init__(
        self,
        channel_name: str,
        sender: Callable[[InboundEnvelope, BotResponse], bool],
    ) -> None:
        self.channel_name = str(channel_name)
        self._sender = sender
        self._on_message: Optional[Callable[[InboundEnvelope], bool]] = None

    def start(self, on_message: Callable[[InboundEnvelope], bool]) -> None:
        self._on_message = on_message

    def stop(self) -> None:
        self._on_message = None

    def emit(self, envelope: InboundEnvelope) -> bool:
        callback = self._on_message
        return bool(callback and callback(envelope))

    def send(self, envelope: InboundEnvelope, response: BotResponse) -> bool:
        return bool(self._sender(envelope, response))


_SECRET_PATTERNS = (
    re.compile(r"(?i)(\b(?:authorization|token|secret|password|passwd|api[_-]?key)\b\s*[:=]\s*)(?:(?:bearer)\s+)?[^\s,;]+"),
    re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)"),
)


def sanitize_outbound_response(response: BotResponse, *, max_chars: Optional[int] = None) -> BotResponse:
    """Redact common credential-shaped fragments before a chat reply.

    Truncation is deliberately *not* applied by default: each Channel Adapter
    knows its own platform limit and splits long replies into segments, so
    clipping here would discard content the adapter would have delivered.
    ``max_chars`` remains available for callers that genuinely need a bound.
    """

    text = str(response.text or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 32].rstrip() + "\n…（消息已截断）"
    if text == response.text:
        return response
    return BotResponse(
        text=text,
        markdown=response.markdown,
        at_user=response.at_user,
        reply_to_message=response.reply_to_message,
        extra=dict(response.extra),
    )


@dataclass(frozen=True)
class ChannelHealth:
    channel: str
    running: bool
    accepted: int
    rejected: int
    duplicate: int
    failed: int
    last_message_at: Optional[datetime]
    last_error: Optional[str]


class ConversationChannelRuntime:
    """Bounded, deduplicating FIFO runtime for personal conversation channels."""

    def __init__(
        self,
        *,
        channel: str,
        on_message: Callable[[BotMessage], Any],
        allowlist: Optional[Set[str]] = None,
        dedupe_ttl_seconds: float = 600.0,
        max_queue_per_conversation: int = 100,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channel = str(channel)
        self._on_message = on_message
        # ``None`` means an explicitly disabled allowlist check; an empty set
        # is intentionally deny-all for personal-channel safe defaults.
        self._allowlist = None if allowlist is None else {str(item) for item in allowlist if str(item)}
        self._dedupe_ttl = max(1.0, float(dedupe_ttl_seconds))
        self._max_queue = max(1, int(max_queue_per_conversation))
        self._clock = clock
        self._lock = threading.RLock()
        self._queues: Dict[str, Deque[InboundEnvelope]] = {}
        self._active: Set[str] = set()
        self._seen: Dict[str, float] = {}
        self._max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix=f"{self.channel}-conversation")
        self._executor_shutdown = False
        self._adapter: Optional[ConversationChannelAdapter] = None
        self._running = False
        self._accepted = 0
        self._rejected = 0
        self._duplicate = 0
        self._failed = 0
        self._last_message_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    def start(self, adapter: Optional[ConversationChannelAdapter] = None) -> None:
        with self._lock:
            if self._running:
                return
            if self._executor_shutdown:
                self._executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix=f"{self.channel}-conversation")
                self._executor_shutdown = False
            self._running = True
            self._adapter = adapter
        if adapter is not None:
            adapter.start(self.receive)

    def stop(self, *, wait: bool = False) -> None:
        adapter: Optional[ConversationChannelAdapter]
        with self._lock:
            self._running = False
            self._queues.clear()
            self._active.clear()
            adapter = self._adapter
            self._adapter = None
        if adapter is not None:
            try:
                adapter.stop()
            except Exception as exc:  # pragma: no cover - adapter-specific
                logger.warning("conversation adapter stop failed: %s", exc)
        self._executor.shutdown(wait=wait)
        self._executor_shutdown = True

    def receive(self, envelope: InboundEnvelope) -> bool:
        """Accept one message, returning false for policy/dedup/backpressure drops."""
        if envelope.channel != self.channel:
            return False
        now = float(self._clock())
        with self._lock:
            if not self._running:
                self._rejected += 1
                return False
            if self._allowlist is not None and envelope.user_id not in self._allowlist:
                self._rejected += 1
                return False
            self._prune_seen(now)
            if envelope.dedupe_key in self._seen:
                self._duplicate += 1
                return False
            queue = self._queues.setdefault(envelope.conversation_key, deque())
            if len(queue) >= self._max_queue:
                self._rejected += 1
                return False
            self._seen[envelope.dedupe_key] = now + self._dedupe_ttl
            queue.append(envelope)
            self._accepted += 1
            self._last_message_at = envelope.timestamp
            if envelope.conversation_key not in self._active:
                self._active.add(envelope.conversation_key)
                try:
                    self._executor.submit(self._drain, envelope.conversation_key)
                except RuntimeError as exc:
                    self._active.discard(envelope.conversation_key)
                    queue.pop()
                    # The message was never handled, so it must not stay
                    # suppressed as a duplicate for the whole dedupe TTL --
                    # otherwise resending the same text is silently ignored.
                    self._seen.pop(envelope.dedupe_key, None)
                    self._failed += 1
                    self._last_error = str(exc)
                    return False
        return True

    def _prune_seen(self, now: float) -> None:
        expired = [key for key, expires_at in self._seen.items() if expires_at <= now]
        for key in expired:
            self._seen.pop(key, None)

    def _drain(self, conversation_key: str) -> None:
        while True:
            with self._lock:
                queue = self._queues.get(conversation_key)
                if not queue:
                    self._queues.pop(conversation_key, None)
                    self._active.discard(conversation_key)
                    return
                envelope = queue.popleft()
            try:
                result = self._on_message(envelope.to_bot_message())
                if inspect.isawaitable(result):
                    result = self._run_awaitable(result)
                if isinstance(result, BotResponse) and result.text and self._adapter is not None:
                    self._adapter.send(envelope, sanitize_outbound_response(result))
            except Exception as exc:  # pragma: no cover - asserted through health
                with self._lock:
                    self._failed += 1
                    self._last_error = str(exc)
                logger.exception("conversation message handling failed")

    @staticmethod
    def _run_awaitable(awaitable: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        result: Dict[str, Any] = {}
        error: Dict[str, BaseException] = {}

        def runner() -> None:
            try:
                result["value"] = asyncio.run(awaitable)
            except BaseException as exc:  # pragma: no cover
                error["value"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if "value" in error:
            raise error["value"]
        return result.get("value")

    def health(self) -> ChannelHealth:
        with self._lock:
            return ChannelHealth(
                channel=self.channel,
                running=self._running,
                accepted=self._accepted,
                rejected=self._rejected,
                duplicate=self._duplicate,
                failed=self._failed,
                last_message_at=self._last_message_at,
                last_error=self._last_error,
            )


__all__ = [
    "CallbackChannelAdapter",
    "ChannelHealth",
    "ConversationChannelAdapter",
    "ConversationChannelRuntime",
    "InboundEnvelope",
    "envelope_from_bot_message",
    "sanitize_outbound_response",
]
