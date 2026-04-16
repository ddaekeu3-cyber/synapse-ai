---
title: "Agent Doesn't Implement Async Observer Pattern"
description: "Agents that use tight polling loops or direct method calls to propagate state changes couple components unnecessarily — an async observer/event-bus pattern lets multiple consumers react to agent events without coupling or blocking."
difficulty: intermediate
category: concurrency
tags: [concurrency, observer, event-bus, pubsub, async, decoupling, architecture]
---

# Agent Doesn't Implement Async Observer Pattern

## Problem

Agent pipelines typically have multiple concerns that must react to the same events: a turn completes → update UI, log metrics, check budget, trigger follow-up. Without an observer pattern, these concerns are wired together as direct calls inside the core agent loop. The result is a tightly coupled monolith: adding a new reaction requires modifying the agent's inner loop, and a slow observer (e.g., a remote log sink) blocks the next turn.

**Symptoms:**
- Agent loop contains scattered logging, metrics, and notification code
- Adding a new downstream consumer (e.g., Slack alert on error) requires editing core logic
- A slow database write in the loop delays the user's next response
- Unit testing the agent core requires mocking all side-effect systems
- Concurrent observers can't receive events independently without shared state

---

## Solution 1: Simple Async Event Bus with asyncio.Queue

A lightweight in-process event bus: producers emit events, consumers each read from their own queue.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import anthropic


@dataclass
class AgentEvent:
    event_type: str   # "turn_start", "turn_end", "tool_call", "error", "budget_warning"
    session_id: str
    data: dict = field(default_factory=dict)


class AsyncEventBus:
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.remove(q)

    async def emit(self, event: AgentEvent) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Slow consumer — drop rather than block


bus = AsyncEventBus()


# --- Observers ---

async def metrics_observer(queue: asyncio.Queue) -> None:
    """Counts tokens, latency per session."""
    totals: dict[str, dict] = {}
    while True:
        event: AgentEvent = await queue.get()
        if event.event_type == "turn_end":
            sid = event.session_id
            totals.setdefault(sid, {"turns": 0, "tokens": 0})
            totals[sid]["turns"] += 1
            totals[sid]["tokens"] += event.data.get("output_tokens", 0)
            print(f"[metrics] {sid}: turns={totals[sid]['turns']} tokens={totals[sid]['tokens']}")


async def logger_observer(queue: asyncio.Queue) -> None:
    """Structured log every event."""
    while True:
        event: AgentEvent = await queue.get()
        print(f"[log] {event.event_type} session={event.session_id} data={event.data}")


async def budget_observer(queue: asyncio.Queue, token_limit: int = 50_000) -> None:
    """Alert when a session exceeds token budget."""
    session_tokens: dict[str, int] = {}
    while True:
        event: AgentEvent = await queue.get()
        if event.event_type == "turn_end":
            sid = event.session_id
            session_tokens[sid] = session_tokens.get(sid, 0) + event.data.get("output_tokens", 0)
            if session_tokens[sid] > token_limit:
                print(f"[budget] ALERT: session={sid} exceeded {token_limit} tokens")


class ObservableAgent:
    def __init__(self, api_key: str, bus: AsyncEventBus):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.bus = bus

    async def chat(self, session_id: str, message: str, history: list[dict]) -> str:
        await self.bus.emit(AgentEvent("turn_start", session_id, {"message": message[:80]}))

        try:
            history.append({"role": "user", "content": message})
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=history,
            )
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})

            await self.bus.emit(AgentEvent("turn_end", session_id, {
                "output_tokens": response.usage.output_tokens,
                "input_tokens": response.usage.input_tokens,
                "stop_reason": response.stop_reason,
            }))
            return reply
        except Exception as exc:
            await self.bus.emit(AgentEvent("error", session_id, {"error": str(exc)}))
            raise


async def demo():
    # Subscribe observers
    q_metrics = await bus.subscribe()
    q_logger = await bus.subscribe()
    q_budget = await bus.subscribe()

    # Start observers as background tasks
    tasks = [
        asyncio.create_task(metrics_observer(q_metrics)),
        asyncio.create_task(logger_observer(q_logger)),
        asyncio.create_task(budget_observer(q_budget, token_limit=100)),
    ]

    agent = ObservableAgent(api_key="sk-...", bus=bus)
    history: list[dict] = []
    for msg in ["Hello!", "What is asyncio?", "Give me an example."]:
        await agent.chat("sess_1", msg, history)

    for t in tasks:
        t.cancel()

# asyncio.run(demo())
```

---

## Solution 2: Typed Event System with Handler Registration

Register strongly-typed handler coroutines per event type; dispatch fires all handlers concurrently.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Type
import anthropic


@dataclass
class BaseEvent:
    session_id: str


@dataclass
class TurnCompletedEvent(BaseEvent):
    user_message: str
    assistant_reply: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


@dataclass
class ToolCalledEvent(BaseEvent):
    tool_name: str
    tool_input: dict
    tool_result: Any


@dataclass
class ErrorEvent(BaseEvent):
    error_type: str
    message: str


EventHandler = Callable[[Any], Coroutine]


class TypedEventDispatcher:
    def __init__(self):
        self._handlers: dict[type, list[EventHandler]] = {}

    def on(self, event_class: type) -> Callable:
        """Decorator: @dispatcher.on(TurnCompletedEvent)"""
        def decorator(fn: EventHandler) -> EventHandler:
            self._handlers.setdefault(event_class, []).append(fn)
            return fn
        return decorator

    async def dispatch(self, event: BaseEvent) -> None:
        handlers = self._handlers.get(type(event), [])
        if handlers:
            await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)


dispatcher = TypedEventDispatcher()


@dispatcher.on(TurnCompletedEvent)
async def log_turn(event: TurnCompletedEvent) -> None:
    print(
        f"[turn] session={event.session_id} "
        f"latency={event.latency_ms:.0f}ms "
        f"tokens={event.input_tokens}+{event.output_tokens}"
    )


@dispatcher.on(TurnCompletedEvent)
async def check_output_length(event: TurnCompletedEvent) -> None:
    if event.output_tokens > 500:
        print(f"[alert] Long output ({event.output_tokens} tokens) for session={event.session_id}")


@dispatcher.on(ErrorEvent)
async def alert_on_error(event: ErrorEvent) -> None:
    print(f"[ERROR] {event.error_type}: {event.message} in session={event.session_id}")


@dispatcher.on(ToolCalledEvent)
async def audit_tool_call(event: ToolCalledEvent) -> None:
    print(f"[audit] Tool={event.tool_name} called in session={event.session_id}")


import time


class TypedObservableAgent:
    def __init__(self, api_key: str, dispatcher: TypedEventDispatcher):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.dispatcher = dispatcher

    async def chat(self, session_id: str, messages: list[dict]) -> str:
        start = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=messages,
            )
            reply = response.content[0].text
            latency = (time.perf_counter() - start) * 1000

            await self.dispatcher.dispatch(TurnCompletedEvent(
                session_id=session_id,
                user_message=messages[-1]["content"][:80],
                assistant_reply=reply[:80],
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                latency_ms=latency,
            ))
            return reply

        except Exception as exc:
            await self.dispatcher.dispatch(ErrorEvent(
                session_id=session_id,
                error_type=type(exc).__name__,
                message=str(exc),
            ))
            raise


async def demo():
    agent = TypedObservableAgent(api_key="sk-...", dispatcher=dispatcher)
    msgs = [{"role": "user", "content": "What is the observer pattern?"}]
    reply = await agent.chat("sess_typed", msgs)
    print(reply[:100])

# asyncio.run(demo())
```

---

## Solution 3: Middleware Chain Observer

Thread observers as middleware in a chain — each middleware can inspect, modify, or react to the request/response before passing it along.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional
import anthropic


@dataclass
class AgentContext:
    session_id: str
    messages: list[dict]
    response: Optional[anthropic.types.Message] = None
    metadata: dict = field(default_factory=dict)  # Shared between middleware


Middleware = Callable[[AgentContext, Callable], Coroutine]


class MiddlewareChain:
    def __init__(self):
        self._middlewares: list[Middleware] = []

    def use(self, middleware: Middleware) -> None:
        self._middlewares.append(middleware)

    async def run(self, ctx: AgentContext, final: Callable) -> None:
        async def build_chain(idx: int) -> None:
            if idx >= len(self._middlewares):
                await final(ctx)
            else:
                await self._middlewares[idx](ctx, lambda: build_chain(idx + 1))

        await build_chain(0)


# --- Middleware implementations ---

async def timing_middleware(ctx: AgentContext, next_fn: Callable) -> None:
    start = time.perf_counter()
    await next_fn()
    elapsed = (time.perf_counter() - start) * 1000
    ctx.metadata["latency_ms"] = elapsed
    print(f"[timing] {ctx.session_id}: {elapsed:.1f}ms")


async def token_tracking_middleware(ctx: AgentContext, next_fn: Callable) -> None:
    await next_fn()
    if ctx.response:
        ctx.metadata["input_tokens"] = ctx.response.usage.input_tokens
        ctx.metadata["output_tokens"] = ctx.response.usage.output_tokens
        print(f"[tokens] in={ctx.response.usage.input_tokens} out={ctx.response.usage.output_tokens}")


async def content_filter_middleware(ctx: AgentContext, next_fn: Callable) -> None:
    """Post-process: check for policy violations in output."""
    await next_fn()
    if ctx.response:
        text = ctx.response.content[0].text if ctx.response.content else ""
        if any(word in text.lower() for word in ["password", "secret", "api key"]):
            print(f"[filter] Sensitive content detected in response for {ctx.session_id}")
            ctx.metadata["flagged"] = True


async def caching_middleware(
    ctx: AgentContext, next_fn: Callable, cache: dict
) -> None:
    cache_key = f"{ctx.session_id}:{ctx.messages[-1]['content'][:100]}"
    if cache_key in cache:
        ctx.response = cache[cache_key]
        ctx.metadata["cache_hit"] = True
        print(f"[cache] Hit for {ctx.session_id}")
        return
    await next_fn()
    if ctx.response:
        cache[cache_key] = ctx.response


class MiddlewareAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.chain = MiddlewareChain()
        self._cache: dict = {}

        self.chain.use(timing_middleware)
        self.chain.use(token_tracking_middleware)
        self.chain.use(content_filter_middleware)
        self.chain.use(lambda ctx, nxt: caching_middleware(ctx, nxt, self._cache))

    async def chat(self, session_id: str, messages: list[dict]) -> str:
        ctx = AgentContext(session_id=session_id, messages=messages)

        async def call_llm(ctx: AgentContext) -> None:
            ctx.response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=ctx.messages,
            )

        await self.chain.run(ctx, lambda: call_llm(ctx))
        return ctx.response.content[0].text if ctx.response and ctx.response.content else ""


async def demo():
    agent = MiddlewareAgent(api_key="sk-...")
    msgs = [{"role": "user", "content": "What is Python?"}]
    r1 = await agent.chat("sess_mw", msgs)
    r2 = await agent.chat("sess_mw", msgs)  # Cache hit
    print(r1[:80])

# asyncio.run(demo())
```

---

## Solution 4: Fan-Out Observer with Per-Observer Error Isolation

Emit to multiple observers concurrently; a crash in one observer never affects others or the main agent.

```python
import asyncio
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine
import anthropic


@dataclass
class AgentEvent:
    event_type: str
    session_id: str
    data: dict = field(default_factory=dict)


class IsolatedFanOutBus:
    def __init__(self):
        self._observers: dict[str, Callable[[AgentEvent], Coroutine]] = {}

    def register(self, name: str, observer: Callable) -> None:
        self._observers[name] = observer
        print(f"[bus] Registered observer: {name}")

    def unregister(self, name: str) -> None:
        self._observers.pop(name, None)

    async def emit(self, event: AgentEvent) -> dict[str, bool]:
        """Fire all observers concurrently; return success map."""
        results = {}

        async def safe_call(name: str, fn: Callable) -> tuple[str, bool]:
            try:
                await fn(event)
                return name, True
            except Exception:
                print(f"[bus] Observer '{name}' failed:\n{traceback.format_exc()[:200]}")
                return name, False

        outcomes = await asyncio.gather(
            *(safe_call(name, fn) for name, fn in self._observers.items())
        )
        return dict(outcomes)


bus = IsolatedFanOutBus()


async def slack_notifier(event: AgentEvent) -> None:
    if event.event_type == "error":
        # Simulate Slack webhook call
        await asyncio.sleep(0.05)
        print(f"[slack] Error in {event.session_id}: {event.data.get('message')}")


async def db_audit_writer(event: AgentEvent) -> None:
    # Simulate DB write (might be slow or fail)
    await asyncio.sleep(0.02)
    if event.event_type == "turn_end":
        print(f"[db] Wrote audit record for {event.session_id}")


async def buggy_observer(event: AgentEvent) -> None:
    raise RuntimeError("This observer is broken!")


bus.register("slack", slack_notifier)
bus.register("db_audit", db_audit_writer)
bus.register("buggy", buggy_observer)  # Won't affect other observers


class FanOutAgent:
    def __init__(self, api_key: str, bus: IsolatedFanOutBus):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.bus = bus

    async def chat(self, session_id: str, message: str) -> str:
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            reply = response.content[0].text
            results = await self.bus.emit(AgentEvent("turn_end", session_id, {
                "output_tokens": response.usage.output_tokens,
            }))
            print(f"[bus] Observer results: {results}")
            return reply
        except Exception as exc:
            await self.bus.emit(AgentEvent("error", session_id, {"message": str(exc)}))
            raise


async def demo():
    agent = FanOutAgent(api_key="sk-...", bus=bus)
    reply = await agent.chat("sess_fanout", "What is fan-out?")
    print(reply[:80])

# asyncio.run(demo())
```

---

## Solution 5: Reactive Stream Observer with asyncio.Condition

Use `asyncio.Condition` to notify multiple observers waiting for specific state transitions.

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import anthropic


class AgentState(Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    TOOL_WAITING = "tool_waiting"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class AgentStateStore:
    state: AgentState = AgentState.IDLE
    last_output: Optional[str] = None
    last_error: Optional[str] = None
    session_id: str = ""


class ReactiveAgentNotifier:
    def __init__(self):
        self._store = AgentStateStore()
        self._condition = asyncio.Condition()

    async def set_state(self, new_state: AgentState, **data) -> None:
        async with self._condition:
            self._store.state = new_state
            for key, val in data.items():
                setattr(self._store, key, val)
            self._condition.notify_all()

    async def wait_for_state(self, target: AgentState, timeout: float = 30.0) -> bool:
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._store.state == target),
                    timeout=timeout,
                )
                return True
            except asyncio.TimeoutError:
                return False

    @property
    def current(self) -> AgentStateStore:
        return self._store


notifier = ReactiveAgentNotifier()


async def ui_updater_observer(notifier: ReactiveAgentNotifier) -> None:
    """Simulates UI that reacts to state changes."""
    while True:
        await notifier.wait_for_state(AgentState.COMPLETE)
        print(f"[ui] Response ready: {notifier.current.last_output[:60] if notifier.current.last_output else ''}")
        # Reset to wait for next complete
        await asyncio.sleep(0.1)


async def error_handler_observer(notifier: ReactiveAgentNotifier) -> None:
    while True:
        await notifier.wait_for_state(AgentState.ERROR)
        print(f"[error_handler] Error detected: {notifier.current.last_error}")
        await asyncio.sleep(0.1)


class ReactiveAgent:
    def __init__(self, api_key: str, notifier: ReactiveAgentNotifier):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.notifier = notifier

    async def chat(self, session_id: str, message: str) -> str:
        await self.notifier.set_state(AgentState.PROCESSING, session_id=session_id)
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            reply = response.content[0].text
            await self.notifier.set_state(AgentState.COMPLETE, last_output=reply)
            return reply
        except Exception as exc:
            await self.notifier.set_state(AgentState.ERROR, last_error=str(exc))
            raise


async def demo():
    agent = ReactiveAgent(api_key="sk-...", notifier=notifier)

    # Start observers
    t1 = asyncio.create_task(ui_updater_observer(notifier))
    t2 = asyncio.create_task(error_handler_observer(notifier))

    await agent.chat("sess_reactive", "What is reactive programming?")
    await asyncio.sleep(0.2)  # Let observers process

    t1.cancel()
    t2.cancel()

# asyncio.run(demo())
```

---

## Solution 6: Persistent Event Log with Replay

Write events to an append-only log; observers can replay from any point — useful for debugging or adding observers after the fact.

```python
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Optional
import anthropic


@dataclass
class LoggedEvent:
    sequence: int
    timestamp: float
    event_type: str
    session_id: str
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class EventLog:
    def __init__(self):
        self._events: list[LoggedEvent] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._new_event = asyncio.Event()

    async def append(self, event_type: str, session_id: str, data: dict) -> LoggedEvent:
        async with self._lock:
            self._seq += 1
            entry = LoggedEvent(
                sequence=self._seq,
                timestamp=time.time(),
                event_type=event_type,
                session_id=session_id,
                data=data,
            )
            self._events.append(entry)
        self._new_event.set()
        self._new_event.clear()
        return entry

    async def tail(self, from_seq: int = 0) -> AsyncIterator[LoggedEvent]:
        """Stream events from from_seq, then follow live."""
        pos = from_seq
        while True:
            async with self._lock:
                batch = [e for e in self._events if e.sequence > pos]
            for event in batch:
                pos = event.sequence
                yield event
            if not batch:
                try:
                    await asyncio.wait_for(asyncio.shield(
                        asyncio.create_task(self._wait_for_new())
                    ), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    async def _wait_for_new(self) -> None:
        await asyncio.sleep(0.05)

    def replay_session(self, session_id: str) -> list[LoggedEvent]:
        return [e for e in self._events if e.session_id == session_id]


event_log = EventLog()


async def audit_tail_observer(log: EventLog, from_seq: int = 0) -> None:
    """Tails the event log and prints a summary."""
    async for event in log.tail(from_seq=from_seq):
        print(f"[audit] seq={event.sequence} type={event.event_type} session={event.session_id}")


class LoggingAgent:
    def __init__(self, api_key: str, log: EventLog):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.log = log

    async def chat(self, session_id: str, message: str) -> str:
        await self.log.append("turn_start", session_id, {"message": message[:80]})
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": message}],
        )
        reply = response.content[0].text
        await self.log.append("turn_end", session_id, {
            "output_tokens": response.usage.output_tokens,
        })
        return reply


async def demo():
    agent = LoggingAgent(api_key="sk-...", log=event_log)

    tail_task = asyncio.create_task(audit_tail_observer(event_log))

    for msg in ["Hello", "What is observability?"]:
        reply = await agent.chat("sess_log", msg)
        print(f"Reply: {reply[:60]}")
        await asyncio.sleep(0.1)

    # Replay session events after the fact
    session_events = event_log.replay_session("sess_log")
    print(f"\n[replay] {len(session_events)} events for sess_log")

    tail_task.cancel()

# asyncio.run(demo())
```

---

## Comparison

| Solution | Coupling | Back-Pressure | Error Isolation | Replay | Complexity |
|---|---|---|---|---|---|
| Async Queue event bus | None | Queue full → drop | Partial | No | Low |
| Typed event dispatcher | None | No | Via gather exc | No | Low |
| Middleware chain | Sequential | N/A | Via try/except | No | Medium |
| Fan-out with isolation | None | No | Yes (per-observer) | No | Low |
| Reactive Condition | Polling-free | N/A | No | No | Medium |
| Persistent event log | None | N/A | No | Yes | Medium |

**Recommendation:** Use Solution 2 (typed event dispatcher) for most agent architectures — it's clean, testable, and type-safe. Add Solution 4 (fan-out with isolation) when any observer might be unreliable (third-party webhooks, external log sinks). Use Solution 6 (persistent log + replay) when you need post-hoc debugging or the ability to attach new observers to historical data.
