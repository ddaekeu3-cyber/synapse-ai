---
title: "Agent Doesn't Implement Graceful Reconnection for Long-Running WebSocket Connections"
description: "Agents that stream responses or maintain persistent channels over WebSocket crash silently when the connection drops, losing in-progress state and leaving users with a frozen interface. Implement graceful reconnection with exponential backoff, message sequence tracking, and state replay to survive connection disruptions."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-reconnection-for-long-running-websocket-connections
tags: [websocket, reconnection, reliability, streaming, state-recovery, long-running]
symptoms:
  - "Agent streaming response stops mid-sentence on network flicker with no recovery"
  - "WebSocket closed event causes unhandled exception that terminates agent loop"
  - "Client receives partial response and hangs indefinitely waiting for more tokens"
  - "Reconnection after network drop starts a new session losing conversation context"
  - "No backoff: agent reconnects at full speed causing server connection storms"
---

## Why This Happens

WebSocket connections are long-lived TCP connections that break on any network event: mobile network switches, load balancer idle timeouts, server restarts, proxy disconnects. Without explicit reconnection logic, the agent's streaming loop simply ends or raises an exception. Proper reconnection requires detecting the disconnect, waiting with jitter-ed exponential backoff, re-establishing the connection with authentication, replaying or resuming in-flight messages, and restoring session state.

## Solution 1: Reconnecting WebSocket Client with Exponential Backoff

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"

@dataclass
class ReconnectPolicy:
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0
    jitter_factor: float = 0.3
    max_attempts: int = 20

    def delay_for(self, attempt: int) -> float:
        base = self.initial_delay_seconds * (self.multiplier ** attempt)
        capped = min(base, self.max_delay_seconds)
        jitter = capped * self.jitter_factor * random.random()
        return capped + jitter

class ReconnectingWebSocketClient:
    """
    Wraps a WebSocket connection with automatic reconnection.
    Calls on_connected() after each (re)connection so the caller can
    re-register subscriptions or resume streaming.
    """

    def __init__(
        self,
        url: str,
        policy: ReconnectPolicy,
        on_connected: Optional[Callable] = None,
        on_message: Optional[Callable] = None,
        headers: Optional[dict] = None,
    ):
        self._url = url
        self._policy = policy
        self._on_connected = on_connected
        self._on_message = on_message
        self._headers = headers or {}
        self._state = ConnectionState.DISCONNECTED
        self._ws = None
        self._attempt = 0
        self._send_queue: asyncio.Queue = asyncio.Queue()

    async def connect_and_run(self) -> None:
        import websockets
        while self._attempt < self._policy.max_attempts:
            try:
                self._state = ConnectionState.CONNECTING
                async with websockets.connect(self._url, extra_headers=self._headers) as ws:
                    self._ws = ws
                    self._state = ConnectionState.CONNECTED
                    self._attempt = 0  # reset on successful connect
                    if self._on_connected:
                        await self._on_connected(ws)
                    await asyncio.gather(
                        self._recv_loop(ws),
                        self._send_loop(ws),
                    )
            except Exception as exc:
                self._state = ConnectionState.RECONNECTING
                delay = self._policy.delay_for(self._attempt)
                print(f"[ws_reconnect] attempt={self._attempt} error={exc} "
                      f"retry_in={delay:.2f}s")
                self._attempt += 1
                await asyncio.sleep(delay)

        self._state = ConnectionState.FAILED
        raise RuntimeError(f"WebSocket failed after {self._policy.max_attempts} attempts")

    async def _recv_loop(self, ws) -> None:
        import websockets
        async for message in ws:
            if self._on_message:
                await self._on_message(message)

    async def _send_loop(self, ws) -> None:
        while True:
            message = await self._send_queue.get()
            await ws.send(message)

    async def send(self, message: str) -> None:
        await self._send_queue.put(message)

    @property
    def state(self) -> ConnectionState:
        return self._state
```

## Solution 2: Message Sequence Tracker for Exactly-Once Delivery

```python
import asyncio
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class SequencedMessage:
    seq: int
    payload: dict
    sent_at: float
    acked: bool = False

class MessageSequenceTracker:
    """
    Assigns sequence numbers to outgoing messages.
    On reconnection, retransmits all unacknowledged messages in order.
    Server must echo back sequence numbers in ACK messages.
    """

    def __init__(self, window_size: int = 100):
        self._next_seq = 0
        self._window: Dict[int, SequencedMessage] = {}
        self._window_size = window_size
        self._lock = asyncio.Lock()

    async def wrap(self, payload: dict) -> str:
        """Returns JSON string with sequence number embedded."""
        import time
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            msg = SequencedMessage(seq=seq, payload=payload, sent_at=time.monotonic())
            self._window[seq] = msg
            # Evict old acked messages
            acked = [s for s, m in self._window.items() if m.acked]
            for s in acked[: -self._window_size]:
                del self._window[s]
        return json.dumps({"seq": seq, **payload})

    async def ack(self, seq: int) -> None:
        async with self._lock:
            if seq in self._window:
                self._window[seq].acked = True

    async def unacked_messages(self) -> List[SequencedMessage]:
        async with self._lock:
            return sorted(
                [m for m in self._window.values() if not m.acked],
                key=lambda m: m.seq,
            )

    async def replay_unacked(self, send_fn) -> int:
        """On reconnection: retransmit all unacknowledged messages."""
        messages = await self.unacked_messages()
        for msg in messages:
            payload_str = json.dumps({"seq": msg.seq, **msg.payload})
            await send_fn(payload_str)
        return len(messages)
```

## Solution 3: Session State Snapshot for Reconnection Resume

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class WebSocketSessionState:
    session_id: str
    conversation_history: List[dict]
    last_message_seq: int
    in_progress_response: Optional[str]   # partial response being streamed
    metadata: Dict[str, Any]
    updated_at: float = field(default_factory=time.time)

class WebSocketSessionStore:
    """Persists session state so reconnection can resume from last checkpoint."""

    def __init__(self, redis):
        self._redis = redis
        self._ttl = 3600  # 1 hour

    async def save(self, state: WebSocketSessionState) -> None:
        key = f"ws_session:{state.session_id}"
        await self._redis.setex(key, self._ttl, json.dumps({
            "session_id": state.session_id,
            "conversation_history": state.conversation_history,
            "last_message_seq": state.last_message_seq,
            "in_progress_response": state.in_progress_response,
            "metadata": state.metadata,
            "updated_at": state.updated_at,
        }))

    async def load(self, session_id: str) -> Optional[WebSocketSessionState]:
        data = await self._redis.get(f"ws_session:{session_id}")
        if not data:
            return None
        d = json.loads(data)
        return WebSocketSessionState(**d)

    async def checkpoint(self, session_id: str, partial_response: str, seq: int) -> None:
        """Update only the in-progress response without full round-trip."""
        state = await self.load(session_id)
        if state:
            state.in_progress_response = partial_response
            state.last_message_seq = seq
            state.updated_at = time.time()
            await self.save(state)


class ResumableStreamingAgent:
    """
    WebSocket agent that checkpoints partial responses.
    On reconnection, resumes streaming from the last checkpoint
    rather than restarting the generation.
    """

    def __init__(
        self,
        ws_client: ReconnectingWebSocketClient,
        session_store: WebSocketSessionStore,
        session_id: str,
    ):
        self._ws = ws_client
        self._store = session_store
        self._session_id = session_id
        self._partial_buffer = ""

    async def on_connected(self, ws) -> None:
        """Called on every (re)connection — sends resume handshake."""
        state = await self._store.load(self._session_id)
        resume_msg = {
            "type": "resume",
            "session_id": self._session_id,
            "last_seq": state.last_message_seq if state else 0,
            "partial_response": state.in_progress_response if state else None,
        }
        await ws.send(json.dumps(resume_msg))

    async def on_token(self, token: str, seq: int) -> None:
        self._partial_buffer += token
        # Checkpoint every 50 tokens
        if len(self._partial_buffer) % 50 == 0:
            await self._store.checkpoint(self._session_id, self._partial_buffer, seq)
```

## Solution 4: Heartbeat Monitor with Auto-Reconnect

```python
import asyncio
import time
from typing import Optional

class WebSocketHeartbeatMonitor:
    """
    Sends periodic ping frames and monitors pong responses.
    If no pong is received within the timeout window, the connection
    is considered dead and reconnection is triggered.
    """

    def __init__(
        self,
        interval_seconds: float = 30.0,
        timeout_seconds: float = 10.0,
    ):
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._last_pong: float = time.monotonic()
        self._ws = None

    def on_pong(self, _data: bytes) -> None:
        self._last_pong = time.monotonic()

    async def monitor_loop(self, ws, disconnect_callback) -> None:
        self._ws = ws
        ws.pong_callback = self.on_pong
        while True:
            await asyncio.sleep(self._interval)
            try:
                await ws.ping()
            except Exception:
                await disconnect_callback()
                return
            await asyncio.sleep(self._timeout)
            elapsed = time.monotonic() - self._last_pong
            if elapsed > self._interval + self._timeout:
                print(f"[ws_heartbeat] no pong in {elapsed:.1f}s, reconnecting")
                await disconnect_callback()
                return

    def is_alive(self) -> bool:
        return (time.monotonic() - self._last_pong) < (self._interval + self._timeout)
```

## Solution 5: Connection Pool for WebSocket Fan-Out

```python
import asyncio
from typing import Dict, List, Optional, Set

class WebSocketConnectionPool:
    """
    Maintains a pool of reconnecting WebSocket connections to multiple endpoints.
    Used when an agent needs to subscribe to multiple upstream streams simultaneously.
    Automatically removes dead connections and reconnects.
    """

    def __init__(self, policy: ReconnectPolicy):
        self._policy = policy
        self._clients: Dict[str, ReconnectingWebSocketClient] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    async def add_connection(
        self,
        name: str,
        url: str,
        on_message,
        on_connected=None,
        headers: Optional[dict] = None,
    ) -> None:
        if name in self._clients:
            return
        client = ReconnectingWebSocketClient(
            url=url, policy=self._policy,
            on_connected=on_connected, on_message=on_message,
            headers=headers,
        )
        self._clients[name] = client
        self._tasks[name] = asyncio.create_task(client.connect_and_run())

    async def remove_connection(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
        self._clients.pop(name, None)

    def status(self) -> Dict[str, str]:
        return {name: client.state.value for name, client in self._clients.items()}

    async def broadcast(self, message: str) -> None:
        await asyncio.gather(*[
            client.send(message) for client in self._clients.values()
            if client.state == ConnectionState.CONNECTED
        ], return_exceptions=True)
```

## Solution 6: Reconnection Metrics and Circuit Breaker

```python
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Deque

@dataclass
class ReconnectionEvent:
    attempt: int
    delay_seconds: float
    success: bool
    error: str
    timestamp: float = field(default_factory=time.time)

class ReconnectionCircuitBreaker:
    """
    Tracks reconnection history. If too many consecutive failures occur
    within a time window, trips the circuit breaker and stops retrying
    until a cooldown expires (avoids endless reconnect storms).
    """

    def __init__(
        self,
        failure_threshold: int = 10,
        window_seconds: float = 120.0,
        cooldown_seconds: float = 300.0,
    ):
        self._threshold = failure_threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._events: Deque[ReconnectionEvent] = deque(maxlen=100)
        self._tripped_at: float = 0.0

    def record(self, event: ReconnectionEvent) -> None:
        self._events.append(event)
        failures = self._recent_failures()
        if failures >= self._threshold:
            self._tripped_at = time.monotonic()
            print(f"[ws_circuit_breaker] TRIPPED after {failures} failures in {self._window}s")

    def _recent_failures(self) -> int:
        cutoff = time.monotonic() - self._window
        return sum(1 for e in self._events if not e.success and e.timestamp > cutoff)

    def is_open(self) -> bool:
        if self._tripped_at == 0.0:
            return False
        elapsed = time.monotonic() - self._tripped_at
        if elapsed >= self._cooldown:
            self._tripped_at = 0.0  # auto-reset after cooldown
            return False
        return True

    def summary(self) -> dict:
        return {
            "total_reconnects": len(self._events),
            "recent_failures": self._recent_failures(),
            "circuit_open": self.is_open(),
            "tripped_at": self._tripped_at or None,
        }
```

## Comparison

| Approach | Recovery Mechanism | State Preservation | Distributed | Overhead |
|---|---|---|---|---|
| ReconnectingWebSocketClient | Exponential backoff + jitter | Via on_connected hook | No | Low |
| MessageSequenceTracker | Unacked message replay | In-memory window | No | Low |
| WebSocketSessionStore | Redis checkpoint + resume | Full state in Redis | Yes | Low (async) |
| WebSocketHeartbeatMonitor | Dead connection detection | None (triggers reconnect) | No | Negligible |
| WebSocketConnectionPool | Per-connection pool | Via individual clients | No | Low |
| ReconnectionCircuitBreaker | Storm prevention | Event history | No | Negligible |

**Best for production**: Combine `ReconnectingWebSocketClient` (backoff) + `WebSocketHeartbeatMonitor` (dead connection detection) + `WebSocketSessionStore` (state resume) + `ReconnectionCircuitBreaker` (storm prevention). Use `MessageSequenceTracker` for any channel where message ordering or exactly-once delivery matters.
