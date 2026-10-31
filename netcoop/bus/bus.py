"""In-process asynchronous message bus (pub/sub + direct addressing).

Agents never share Python objects: `publish` serializes every envelope to its JSON wire
form, and each recipient gets its own copy decoded (version-checked, migrated, validated)
from that string. External agents use exactly the same path via `publish_wire`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from netcoop.bus.log import MessageLog
from netcoop.protocol.envelope import (
    BROADCAST,
    SYSTEM_CORRELATION,
    Envelope,
    ProtocolError,
    decode_message,
    make_message,
)
from netcoop.protocol.payloads import ErrorPayload, utcnow
from netcoop.protocol.vocabulary import MessageType

logger = logging.getLogger("netcoop.bus")

Handler = Callable[[Envelope], Awaitable[None]]
Tap = Callable[[Envelope], None]
BUS_NAME = "bus"


@dataclass
class _Subscriber:
    name: str
    handler: Handler
    types: frozenset[MessageType] | None
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None


@dataclass
class DeadLetter:
    reason: str
    wire: str


class MessageBus:
    def __init__(self, log: MessageLog | None = None) -> None:
        self.log = log or MessageLog()
        self._subs: dict[str, _Subscriber] = {}
        self._taps: list[Tap] = []
        self._retained: dict[str, str] = {}  # agent -> last CAPABILITY_ANNOUNCE wire
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._tracked: set[asyncio.Task[Any]] = set()
        self.dead_letters: list[DeadLetter] = []
        self.stats: Counter[str] = Counter()

    # -- membership -------------------------------------------------------------------------

    def subscribe(self, name: str, handler: Handler, types: frozenset[MessageType] | None = None) -> None:
        """Register `name`. Broadcasts are filtered by `types`; direct messages always arrive."""
        if name in self._subs:
            raise ValueError(f"agent name already registered on the bus: {name}")
        sub = _Subscriber(name=name, handler=handler, types=types)
        sub.task = asyncio.get_running_loop().create_task(self._consume(sub), name=f"bus:{name}")
        self._subs[name] = sub
        # Retained capability announcements, so late joiners learn about earlier agents.
        if types is None or MessageType.CAPABILITY_ANNOUNCE in types:
            for agent, wire in self._retained.items():
                if agent != name:
                    self._enqueue(sub, wire)

    async def unsubscribe(self, name: str) -> None:
        sub = self._subs.pop(name, None)
        if sub and sub.task:
            sub.task.cancel()
            try:
                await sub.task
            except asyncio.CancelledError:
                pass
            # Anything still queued for it is no longer in flight.
            while not sub.queue.empty():
                sub.queue.get_nowait()
                self._done()
        self._retained.pop(name, None)

    @property
    def members(self) -> list[str]:
        return sorted(self._subs)

    def add_tap(self, tap: Tap) -> None:
        """Observe every accepted message (CLI live feed, dashboard)."""
        self._taps.append(tap)

    # -- publishing -------------------------------------------------------------------------

    async def publish(self, envelope: Envelope) -> None:
        await self.publish_wire(envelope.to_wire())

    async def publish_wire(self, wire: str | bytes) -> Envelope | None:
        """Accept a message in wire form. Invalid messages are answered with an ERROR."""
        text = wire.decode("utf-8", errors="replace") if isinstance(wire, bytes) else wire
        try:
            envelope = decode_message(text)
        except ProtocolError as exc:
            self.stats["rejected"] += 1
            self.dead_letters.append(DeadLetter(f"{exc.code}: {exc.message}", text[:2000]))
            await self._error_to_sender(text, exc)
            return None

        self.stats["published"] += 1
        self.stats[f"type:{envelope.type}"] += 1
        self.log.append(envelope, text)
        for tap in self._taps:
            try:
                tap(envelope)
            except Exception:  # a broken observer must never break delivery
                logger.exception("bus tap failed")

        if envelope.type is MessageType.CAPABILITY_ANNOUNCE:
            self._retained[envelope.from_agent] = text

        for sub in self._recipients(envelope):
            self._enqueue(sub, text)
        return envelope

    def _recipients(self, envelope: Envelope) -> list[_Subscriber]:
        if envelope.to_agent != BROADCAST:
            sub = self._subs.get(envelope.to_agent)
            if sub is None:
                self.stats["dead_letter"] += 1
                self.dead_letters.append(DeadLetter(f"no such agent: {envelope.to_agent}", envelope.to_wire()))
                return []
            return [sub]
        return [
            sub
            for sub in self._subs.values()
            if sub.name != envelope.from_agent and (sub.types is None or envelope.type in sub.types)
        ]

    async def _error_to_sender(self, text: str, exc: ProtocolError) -> None:
        sender = _best_effort_field(text, "from_agent") or _best_effort_field(text, "sender")
        correlation = _best_effort_field(text, "correlation_id") or SYSTEM_CORRELATION
        error = make_message(
            MessageType.ERROR,
            ErrorPayload(
                code=exc.code,
                message=exc.message,
                detail={k: str(v) for k, v in exc.detail.items()},
                offending_msg_id=_best_effort_field(text, "msg_id"),
                recoverable=True,
            ),
            from_agent=BUS_NAME,
            to_agent=sender if sender in self._subs else BROADCAST,
            correlation_id=correlation,
        )
        await self.publish(error)

    # -- delivery ---------------------------------------------------------------------------

    def _enqueue(self, sub: _Subscriber, wire: str) -> None:
        self._inflight += 1
        self._idle.clear()
        sub.queue.put_nowait(wire)

    def _done(self) -> None:
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def _consume(self, sub: _Subscriber) -> None:
        while True:
            wire = await sub.queue.get()
            try:
                envelope = decode_message(wire)  # each recipient decodes its own copy
                if utcnow() > envelope.timestamp + timedelta(seconds=envelope.ttl_s):
                    self.stats["expired"] += 1
                    self.dead_letters.append(DeadLetter("ttl expired", wire))
                    continue
                self.stats[f"delivered:{sub.name}"] += 1
                await sub.handler(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:  # handlers contain their own errors; this is a last resort
                logger.exception("handler for %s failed", sub.name)
                self.stats["handler_crash"] += 1
            finally:
                self._done()

    # -- background work tracked for quiescence ------------------------------------------------

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task[Any]:
        """Run `coro` as a task that counts as in-flight work for `drain()`."""
        self._inflight += 1
        self._idle.clear()
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tracked.add(task)

        def _finished(t: asyncio.Task[Any]) -> None:
            self._tracked.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background task %s failed: %r", t.get_name(), t.exception())
            self._done()

        task.add_done_callback(_finished)
        return task

    async def drain(self, timeout: float | None = 30.0) -> bool:
        """Wait until no message is queued or being handled. Returns False on timeout."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def close(self) -> None:
        for name in list(self._subs):
            await self.unsubscribe(name)
        for task in list(self._tracked):
            task.cancel()


def _best_effort_field(text: str, key: str) -> str | None:
    """Pull a string field out of possibly-invalid JSON, for addressing error replies."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        value = data.get(key)
        return value if isinstance(value, str) else None
    return None
