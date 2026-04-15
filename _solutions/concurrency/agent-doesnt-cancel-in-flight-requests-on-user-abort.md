---
layout: solution
title: "Agent Doesn't Cancel In-Flight Requests When User Aborts"
category: concurrency
description: "A user presses Stop or navigates away while the agent is mid-execution. The HTTP connection closes, but the agent's background LLM calls, tool executions, and database writes continue running — wasting tokens, consuming rate limit quota, and potentially mutating state the user intended to abandon. The fix is to propagate cancellation signals through every layer of the agent's execution."
tags: [concurrency, cancellation, abort, resource-leak, token-waste, asyncio, background-tasks]
---

# Agent Doesn't Cancel In-Flight Requests When User Aborts

## Symptom
- User hits Stop — agent keeps processing for 30+ more seconds, billing tokens
- User navigates away — background database writes from the aborted request still execute
- Rate limit quota consumed by in-flight requests that the user already cancelled
- Agent spawns subtasks; user abort cancels the parent but orphaned subtasks run to completion
- Server logs show completed agent runs for sessions that were closed minutes earlier
- Long-running tool calls (web scraping, file processing) continue after abort

## Root Cause
Agent loops typically run as fire-and-forget coroutines or threads. When the HTTP connection closes, the framework signals cancellation at the transport layer, but this signal doesn't propagate into the agent's internal async tasks or thread pool workers. Without explicit cancellation handling at each await point and tool call, the agent runs to completion regardless of whether anyone is waiting for the result. The fix is to use `asyncio.CancelledError` / cancellation tokens to propagate abort signals through the entire call chain.

## Fix

### Option 1: asyncio cancellation — cancel the agent task when request is aborted

```python
import anthropic
import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

class CancellableAgentRunner:
    """
    Runs the agent as a cancellable asyncio.Task.
    When the caller cancels the task, in-flight LLM calls and tool executions
    are cleanly cancelled via asyncio's cooperative cancellation protocol.
    """

    def __init__(self):
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def run(self, session_id: str, user_message: str, tools: list[dict], tool_handler) -> str:
        """
        Run agent; store the task so it can be cancelled externally.
        """
        task = asyncio.create_task(
            self._agent_loop(user_message, tools, tool_handler),
            name=f"agent-{session_id}"
        )
        self._active_tasks[session_id] = task
        try:
            result = await task
            return result
        except asyncio.CancelledError:
            logger.info(f"Agent task {session_id} was cancelled cleanly")
            raise
        finally:
            self._active_tasks.pop(session_id, None)

    def cancel(self, session_id: str) -> bool:
        """Cancel an in-flight agent run by session ID."""
        task = self._active_tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            logger.info(f"Cancellation requested for session {session_id}")
            return True
        return False

    async def _agent_loop(self, user_message: str, tools: list[dict], tool_handler) -> str:
        messages = [{"role": "user", "content": user_message}]

        for turn in range(20):
            # Each await is a cancellation point — CancelledError propagates here:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                tools=tools,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                return response.content[0].text

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Tool calls are also cancellable if they're async:
                    result = await asyncio.shield(
                        asyncio.get_event_loop().run_in_executor(
                            None, tool_handler, block.name, block.input
                        )
                    )
                    # Note: asyncio.shield protects from cancellation during cleanup-critical ops.
                    # Remove shield for operations that SHOULD abort on cancel.
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "Max turns reached."


# FastAPI integration:
# runner = CancellableAgentRunner()
#
# @app.post("/chat/{session_id}")
# async def chat(session_id: str, body: ChatRequest, request: Request):
#     async def on_disconnect():
#         if await request.is_disconnected():
#             runner.cancel(session_id)
#     asyncio.create_task(on_disconnect())
#     try:
#         return await runner.run(session_id, body.message, TOOLS, handle_tool)
#     except asyncio.CancelledError:
#         return {"status": "cancelled"}
```

### Option 2: Cancellation token — propagate abort through synchronous code

```python
import anthropic
import threading
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class CancellationToken:
    """
    A cancellation token that can be passed through synchronous code.
    Each operation checks the token before proceeding.
    """
    _cancelled: bool = False
    _reason: str = ""

    def cancel(self, reason: str = "user_abort") -> None:
        self._cancelled = True
        self._reason = reason
        logger.info(f"CancellationToken cancelled: {reason}")

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        """Raise if cancelled. Call at each checkpoint."""
        if self._cancelled:
            raise InterruptedError(f"Operation cancelled: {self._reason}")


def run_tool_with_cancellation(
    tool_name: str,
    tool_inputs: dict,
    ct: CancellationToken
) -> str:
    """Run a tool, checking for cancellation between steps."""
    ct.check()   # ← check before starting
    logger.debug(f"Running tool {tool_name}...")

    # Simulate a multi-step tool (e.g., web scraping):
    for step in range(3):
        ct.check()  # ← check between steps
        time.sleep(0.1)  # simulated work

    ct.check()   # ← check after completion but before returning
    return f"Tool {tool_name} result"


def run_agent_with_cancellation(
    user_message: str,
    tools: list[dict],
    ct: CancellationToken
) -> str:
    """
    Synchronous agent loop that checks the cancellation token at each step.
    """
    messages = [{"role": "user", "content": user_message}]

    for turn in range(20):
        ct.check()  # ← check before each LLM call

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        ct.check()  # ← check after LLM call returns

        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = run_tool_with_cancellation(block.name, block.input, ct)
                except InterruptedError:
                    logger.info(f"Tool {block.name} cancelled")
                    raise
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Max turns reached."


def run_with_timeout_and_cancel(user_message: str, timeout: float = 30.0) -> str:
    """Run agent in a thread; cancel if the caller disconnects."""
    ct = CancellationToken()
    result = {"value": None, "error": None}

    def worker():
        try:
            result["value"] = run_agent_with_cancellation(user_message, [], ct)
        except InterruptedError as e:
            result["error"] = str(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        # Timed out or caller disconnected — signal cancellation:
        ct.cancel(reason="timeout")
        thread.join(timeout=5.0)  # give thread time to clean up
        return "Request timed out. Please try again."

    if result["error"]:
        return "Request was cancelled."

    return result["value"] or "No response"
```

### Option 3: Streaming with abort — stop token generation on disconnect

```python
import anthropic
import asyncio
import logging

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

async def stream_with_cancellation(
    user_message: str,
    abort_event: asyncio.Event,
    on_token: callable = print
) -> str:
    """
    Stream tokens to the caller. If abort_event is set, stop streaming
    and close the stream — saving tokens from the point of abort onwards.
    """
    collected_text = []

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        async for text in stream.text_stream:
            if abort_event.is_set():
                logger.info("Stream aborted by user — stopping token generation")
                # Breaking here cancels the stream, stopping billing at this point:
                break
            on_token(text)
            collected_text.append(text)

    return "".join(collected_text)


async def demo_abort_streaming():
    """Simulate user aborting a stream after 0.5 seconds."""
    abort = asyncio.Event()

    async def abort_after_delay():
        await asyncio.sleep(0.5)
        abort.set()
        print("\n[User aborted]")

    tokens_received = []

    abort_task = asyncio.create_task(abort_after_delay())
    result = await stream_with_cancellation(
        user_message="Write a very long essay about the history of computing.",
        abort_event=abort,
        on_token=lambda t: tokens_received.append(t)
    )
    abort_task.cancel()

    print(f"\nTokens received before abort: {len(tokens_received)}")
    print(f"Partial result: {''.join(tokens_received)[:100]}...")
```

### Option 4: Resource cleanup on cancellation — prevent partial writes

```python
import anthropic
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

class CancelSafeTransaction:
    """
    Context manager that rolls back mutations if the operation is cancelled.
    Use for any state-mutating operations (DB writes, file writes, API calls).
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._pending_writes: list[dict] = []
        self._committed = False

    def stage_write(self, operation: str, data: dict) -> None:
        """Stage a write — it will only be committed if the operation completes."""
        self._pending_writes.append({"op": operation, "data": data})
        logger.debug(f"Staged write: {operation}")

    def commit(self) -> None:
        """Commit all staged writes."""
        for write in self._pending_writes:
            logger.info(f"Committing: {write['op']}")
            # actual_db_write(write['data'])
        self._committed = True
        logger.info(f"Transaction committed: {len(self._pending_writes)} writes")

    def rollback(self) -> None:
        """Discard all staged writes."""
        if self._pending_writes:
            logger.info(f"Rolling back {len(self._pending_writes)} staged writes")
        self._pending_writes.clear()


@asynccontextmanager
async def cancellable_agent_transaction(session_id: str) -> AsyncGenerator:
    """
    Runs the agent body in a transaction that rolls back on cancellation.
    Prevents partial state mutations from aborted runs.
    """
    txn = CancelSafeTransaction(session_id)
    try:
        yield txn
        txn.commit()
    except asyncio.CancelledError:
        txn.rollback()
        logger.info(f"Agent cancelled for session {session_id} — rolled back")
        raise
    except Exception:
        txn.rollback()
        raise


async def agent_with_safe_writes(session_id: str, user_message: str) -> str:
    """
    Agent that stages all writes in a transaction.
    If cancelled, no partial writes reach the database.
    """
    async with cancellable_agent_transaction(session_id) as txn:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}]
        )
        reply = response.content[0].text

        # Stage writes — not committed until the context manager exits cleanly:
        txn.stage_write("update_session", {"session_id": session_id, "last_reply": reply})
        txn.stage_write("log_interaction", {"session_id": session_id, "tokens": response.usage.input_tokens})

        return reply
```

### Option 5: Cancellation registry — track and cancel all subtasks

```python
import anthropic
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

@dataclass
class CancellationRegistry:
    """
    Tracks all async tasks spawned by an agent run.
    Cancelling the registry cancels every registered task.
    """
    session_id: str
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _cancelled: bool = False

    def register(self, task: asyncio.Task) -> asyncio.Task:
        """Register a task for bulk cancellation."""
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)
        return task

    async def cancel_all(self, reason: str = "user_abort") -> int:
        """Cancel all registered tasks. Returns count of tasks cancelled."""
        self._cancelled = True
        count = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                count += 1
        if count:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info(f"[{self.session_id}] Cancelled {count} tasks: {reason}")
        return count

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


async def parallel_tool_calls(
    blocks: list,
    tool_handler,
    registry: CancellationRegistry
) -> list[dict]:
    """
    Run tool calls in parallel, all registered with the cancellation registry.
    If the registry is cancelled, all tool calls abort.
    """
    async def run_one(block):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, tool_handler, block.name, block.input)

    tasks = [
        registry.register(asyncio.create_task(run_one(block)))
        for block in blocks
        if block.type == "tool_use"
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    tool_results = []
    for block, result in zip(
        [b for b in blocks if b.type == "tool_use"], results
    ):
        if isinstance(result, (asyncio.CancelledError, Exception)):
            content = f"Tool cancelled or failed: {result}"
        else:
            content = str(result)
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content
        })
    return tool_results


# Active registries indexed by session_id:
_registries: dict[str, CancellationRegistry] = {}


async def run_agent_with_registry(session_id: str, user_message: str) -> str:
    registry = CancellationRegistry(session_id=session_id)
    _registries[session_id] = registry

    try:
        messages = [{"role": "user", "content": user_message}]
        for _ in range(20):
            if registry.is_cancelled:
                return "Request was cancelled."

            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                return response.content[0].text

            tool_results = await parallel_tool_calls(
                response.content,
                lambda name, inputs: f"result of {name}",
                registry
            )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "Max turns."
    finally:
        _registries.pop(session_id, None)


def abort_session(session_id: str) -> bool:
    """Called by the HTTP disconnect handler or Stop button."""
    registry = _registries.get(session_id)
    if registry:
        asyncio.create_task(registry.cancel_all(reason="user_abort"))
        return True
    return False
```

### Option 6: Token budget enforcement — limit spend even without cancellation signal

```python
import anthropic
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

class TokenBudgetEnforcer:
    """
    Even when cancellation signals don't propagate (e.g., in sync code
    or multi-process setups), enforce a hard token budget so aborted
    requests can't spend unbounded tokens.
    """
    def __init__(self, max_input_tokens: int = 50_000, max_output_tokens: int = 10_000):
        self.max_input = max_input_tokens
        self.max_output = max_output_tokens
        self.used_input = 0
        self.used_output = 0

    @property
    def input_remaining(self) -> int:
        return max(0, self.max_input - self.used_input)

    @property
    def output_remaining(self) -> int:
        return max(0, self.max_output - self.used_output)

    @property
    def budget_exhausted(self) -> bool:
        return self.used_input >= self.max_input or self.used_output >= self.max_output

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.used_input += input_tokens
        self.used_output += output_tokens
        if self.budget_exhausted:
            logger.warning(
                f"Token budget exhausted: {self.used_input}/{self.max_input} input, "
                f"{self.used_output}/{self.max_output} output"
            )


def budget_bounded_agent(user_message: str, max_turns: int = 10) -> str:
    budget = TokenBudgetEnforcer(max_input_tokens=20_000, max_output_tokens=5_000)
    messages = [{"role": "user", "content": user_message}]

    for turn in range(max_turns):
        if budget.budget_exhausted:
            logger.warning(f"Stopping agent after {turn} turns: budget exhausted")
            return "I've reached my processing limit for this request. Please try a simpler query."

        # Dynamically reduce max_tokens as budget shrinks:
        safe_max_tokens = min(512, budget.output_remaining)
        if safe_max_tokens < 32:
            return "Token budget too low to continue."

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=safe_max_tokens,
            messages=messages
        )
        budget.record(response.usage.input_tokens, response.usage.output_tokens)

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": []})

    return "Max turns reached."
```

## Cancellation Propagation Checklist

| Layer | Signal | How to Cancel |
|---|---|---|
| HTTP transport | Client disconnect | FastAPI `request.is_disconnected()` |
| asyncio task | `CancelledError` | `task.cancel()` at each `await` |
| Synchronous thread | Cancellation token | Check token at each loop iteration |
| LLM streaming | Close stream | Break from `async for` loop |
| Tool execution | Cancellation token / thread interrupt | Pass token to tool; check before each step |
| Subtasks | Cancellation registry | `registry.cancel_all()` |
| Writes / mutations | Transaction rollback | `CancelSafeTransaction.rollback()` |

## Expected Token Savings
Cancelling a streaming response at 500 tokens instead of letting it run to 2000 tokens saves ~1500 output tokens per abort (~$0.02 at Sonnet pricing). For high-traffic applications where users frequently abort, this adds up to significant cost reduction. More importantly, it prevents rate-limit quota from being consumed by requests nobody is waiting for.

## Environment
- Web-facing agents where users can close tabs or press Stop; async FastAPI/Starlette deployments (use `asyncio.CancelledError`); synchronous WSGI deployments (use cancellation tokens); streaming responses (check abort_event in token loop); multi-step agents that write to databases (use transactional rollback on cancellation); token budget enforcement (Option 6) is a universal fallback that works even when cancellation signals can't propagate
