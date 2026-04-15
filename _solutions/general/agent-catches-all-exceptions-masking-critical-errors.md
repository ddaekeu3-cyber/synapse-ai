---
layout: solution
title: "Agent catches all exceptions masking critical errors"
category: general
description: "Bare `except: pass` or `except Exception` blocks silently swallow authentication failures, out-of-memory errors, and corrupted state — the agent appears to run but produces wrong answers with no visible signal."
tags: [error-handling, exception, silent-failure, debugging, observability, bare-except]
---

## Symptom

The agent runs without crashing but returns empty, default, or stale results. No errors appear in logs. Increasing log verbosity still shows nothing — the exception was caught and discarded at the source. Debugging requires adding print statements at every possible failure point to find where the silent failure occurs. Once found, the root cause turns out to be an authentication failure, network timeout, or disk-full error that was caught and ignored.

## Root Cause

The original developer added a broad `except` block to handle one specific error (e.g., a transient network blip) and used `pass` or a generic log message. Over time, this catch-all intercepts unrelated critical exceptions: `KeyboardInterrupt`, `MemoryError`, API authentication failures, and disk write errors. Python's `except Exception` catches everything except `BaseException` subclasses like `SystemExit` — even `MemoryError` is a subclass of `Exception` in Python 3, making it catchable and suppressable.

---

## Option 1 — Catch only specific, expected exceptions

**Replace `except Exception` with explicit exception types. Let unexpected exceptions propagate.**

```python
import anthropic

client = anthropic.Anthropic()


def call_llm_bad(prompt: str) -> str:
    """BAD: swallows everything including auth failures, OOM, etc."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception:
        return ""   # ← silent failure; caller has no idea what went wrong


def call_llm_good(prompt: str) -> str:
    """GOOD: only catches transient, recoverable errors."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    except anthropic.RateLimitError as e:
        # Transient — caller can retry
        raise RuntimeError(f"Rate limited: {e}") from e

    except anthropic.APIConnectionError as e:
        # Transient network issue — caller can retry
        raise RuntimeError(f"Connection error: {e}") from e

    # Do NOT catch:
    # - anthropic.AuthenticationError  → config bug, must be fixed
    # - anthropic.BadRequestError      → prompt bug, must be fixed
    # - MemoryError                    → system resource issue
    # - KeyboardInterrupt              → user wants to stop


def safe_batch(prompts: list[str]) -> list[str]:
    results = []
    for i, p in enumerate(prompts):
        try:
            results.append(call_llm_good(p))
        except RuntimeError as e:
            print(f"  Prompt {i} failed (recoverable): {e}")
            results.append(f"[Error: {e}]")
        # Let auth errors, OOM, etc. propagate up — they need fixing
    return results


results = safe_batch(["What is 2+2?", "Name a planet.", "What colour is the sky?"])
for r in results:
    print(r[:60])
```

**Expected Token Savings:** Propagating auth errors immediately prevents the agent from running hundreds of requests against an invalid API key — saves 100% of tokens on an invalid-key session vs. silently returning empty strings.

**Environment:** Any agent codebase; the most impactful single change for observability.

---

## Option 2 — Exception hierarchy: recoverable vs. fatal classification

**Classify exceptions at the point of catch: retry recoverable ones, log-and-reraise fatal ones.**

```python
import logging
import anthropic

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Exceptions that are safe to retry (transient)
RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
)

# Exceptions that indicate a bug or config error (fatal — must propagate)
FATAL = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.BadRequestError,
)

client = anthropic.Anthropic()


def call_llm(prompt: str, attempt: int = 1) -> str:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    except FATAL as e:
        # Log with full context and re-raise — do NOT swallow
        log.critical("Fatal API error (will not retry): %s — %s", type(e).__name__, e)
        raise   # re-raise original; don't convert to RuntimeError

    except RETRYABLE as e:
        if attempt >= 3:
            log.error("Max retries reached for '%s': %s", prompt[:40], e)
            raise
        log.warning("Transient error on attempt %d: %s — retrying", attempt, e)
        import time
        time.sleep(2 ** attempt)
        return call_llm(prompt, attempt + 1)

    except anthropic.RateLimitError as e:
        import time
        retry_after = float(e.response.headers.get("retry-after", 5))
        log.warning("Rate limited — sleeping %.1fs", retry_after)
        time.sleep(retry_after)
        return call_llm(prompt, attempt)

    # NOTE: no bare `except` — unknown exceptions propagate to the caller


try:
    result = call_llm("Explain quantum entanglement in one sentence.")
    print(result)
except anthropic.AuthenticationError:
    print("FATAL: Check ANTHROPIC_API_KEY environment variable.")
    raise SystemExit(1)
```

**Expected Token Savings:** Immediate fatal escalation stops the agent before it burns tokens on a misconfigured session. For a 1,000-task batch with a bad API key, saves all 1,000 task tokens vs. silently failing and retrying.

**Environment:** Any agent; replace `RETRYABLE`/`FATAL` sets with the SDK exceptions relevant to your API clients.

---

## Option 3 — Structured error context for debugging

**When catching broad exceptions, always log full context before re-raising — never use bare `pass`.**

```python
import json
import logging
import traceback
import anthropic

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

client = anthropic.Anthropic()


class AgentError(Exception):
    """Wraps any agent error with debugging context."""
    def __init__(self, message: str, context: dict):
        super().__init__(message)
        self.context = context

    def __str__(self) -> str:
        return f"{super().__str__()} | context={json.dumps(self.context, default=str)}"


def process_task(task_id: str, prompt: str) -> str:
    context = {
        "task_id":     task_id,
        "prompt_len":  len(prompt),
        "prompt_head": prompt[:50],
    }
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    except anthropic.APIError as e:
        context["status_code"] = getattr(e, "status_code", None)
        context["error_type"]  = type(e).__name__
        log.error("API error on task %s: %s", task_id, e, extra={"context": context})
        raise AgentError(f"API error: {e}", context) from e

    except Exception as e:
        # Broad catch ONLY for logging — always re-raise
        context["error_type"] = type(e).__name__
        context["traceback"]  = traceback.format_exc()
        log.critical(
            "Unexpected error on task %s: %s\n%s",
            task_id, e, context["traceback"]
        )
        raise   # NEVER use `pass` here — re-raise original exception


try:
    result = process_task("task-001", "What is the speed of light?")
    print(result)
except AgentError as e:
    print(f"Task failed with context: {e}")
```

**Expected Token Savings:** Structured error context reduces debugging time from hours to minutes — faster debugging means fewer diagnostic LLM calls made by developers trying to reproduce the bug.

**Environment:** Any production agent; the pattern is: catch broadly for logging only, always re-raise.

---

## Option 4 — Exception allow-list via decorator

**Decorate tool handlers with an explicit allow-list of catchable exceptions. Any unlisted exception propagates.**

```python
import functools
import logging
from typing import Callable, Type
import anthropic

log = logging.getLogger(__name__)
client = anthropic.Anthropic()


def catches(*allowed_exceptions: Type[Exception], default=None):
    """
    Decorator: only catch exceptions in `allowed_exceptions`.
    All others propagate. Returns `default` on caught exception.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except allowed_exceptions as e:
                log.warning("%s: caught %s: %s", fn.__name__, type(e).__name__, e)
                return default
            # No bare except — everything else propagates
        return wrapper
    return decorator


@catches(anthropic.APIConnectionError, anthropic.APITimeoutError, default="[unavailable]")
def fetch_summary(topic: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise: {topic}"}],
    )
    return response.content[0].text


@catches(KeyError, ValueError, default={})
def parse_config(raw: dict) -> dict:
    return {
        "model":      raw["model"],
        "max_tokens": int(raw["max_tokens"]),
        "timeout":    float(raw.get("timeout", "30")),
    }


# Connection errors return "[unavailable]" — auth errors still propagate
result = fetch_summary("machine learning")
print(f"Summary: {result}")

# KeyError/ValueError return {} — other exceptions propagate
config = parse_config({"model": "claude-haiku-4-5-20251001", "max_tokens": "256"})
print(f"Config: {config}")

# This WILL raise (not in the allow-list) — good!
try:
    bad_config = parse_config({"max_tokens": "not-a-number"})
except ValueError as e:
    print(f"ValueError propagated correctly: {e}")
```

**Expected Token Savings:** Explicit allow-list prevents accidental suppression of auth and config errors — eliminates the debugging sessions (3–10 diagnostic API calls each) needed to find silently-failing agents.

**Environment:** Tool handler functions that need to be resilient to one class of errors but must propagate others; self-documenting via the decorator signature.

---

## Option 5 — Global exception hook for unhandled exceptions in background tasks

**Register a global handler for unhandled exceptions that would otherwise be silently swallowed by `asyncio` or `threading`.**

```python
import asyncio
import logging
import sys
import threading
import anthropic

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)

client = anthropic.AsyncAnthropic()


def setup_global_exception_hooks() -> None:
    """Install hooks to log exceptions that would otherwise be silently dropped."""

    # 1. threading: catch exceptions in threads (Python 3.8+)
    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical(
            "Unhandled exception in thread '%s':",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_exception_hook

    # 2. asyncio: catch exceptions in tasks that are never awaited
    def asyncio_exception_handler(loop, context: dict) -> None:
        exc  = context.get("exception")
        msg  = context.get("message", "No message")
        task = context.get("task")

        if exc is None:
            log.error("asyncio error: %s", msg)
            return

        if isinstance(exc, asyncio.CancelledError):
            return   # expected

        log.critical(
            "Unhandled asyncio exception in task '%s': %s",
            task.get_name() if task else "unknown",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    asyncio.get_event_loop().set_exception_handler(asyncio_exception_handler)

    # 3. sys.excepthook: catch any exception that reaches the top level
    original_hook = sys.excepthook
    def global_excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            original_hook(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = global_excepthook


async def llm_task(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main() -> None:
    setup_global_exception_hooks()

    # Task that raises — without the hook, this would be silently dropped
    bad_task = asyncio.create_task(
        asyncio.coroutine(lambda: (_ for _ in ()).throw(RuntimeError("simulated unhandled error")))()
    )

    good_task = asyncio.create_task(llm_task("What is 2+2?"))
    result = await good_task
    print(f"Good task result: {result}")

    try:
        await bad_task
    except RuntimeError:
        pass   # already logged by the hook


asyncio.run(main())
```

**Expected Token Savings:** Global hooks ensure no exception is ever silently dropped in background tasks — enables immediate detection and correction of failing agents rather than discovering the failure hours later after thousands of wasted API calls.

**Environment:** asyncio-based agents; install hooks once at startup before any tasks are created.

---

## Option 6 — Static analysis: detect bare `except` and `except Exception: pass` in CI

**Use `flake8` with `bugbear` or `pylint` to flag dangerous exception patterns before they reach production.**

```python
# scripts/check_exceptions.py
"""
Scan Python files for dangerous exception patterns.
Run: python scripts/check_exceptions.py src/
"""
import ast
import sys
import os


DANGEROUS_PATTERNS = []


def check_file(filepath: str) -> list[tuple[int, str]]:
    with open(filepath) as f:
        source = f.read()

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        # bare `except:` — catches BaseException including SystemExit
        if node.type is None:
            issues.append((node.lineno, "bare `except:` — catches SystemExit and KeyboardInterrupt"))
            continue

        # `except Exception:` with only `pass` body
        if (isinstance(node.type, ast.Name) and node.type.id == "Exception"
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Pass)):
            issues.append((node.lineno, "`except Exception: pass` — silently swallows all errors"))
            continue

        # `except Exception as e: pass` or just logging with no re-raise
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            has_raise  = any(isinstance(n, ast.Raise)  for n in ast.walk(ast.Module(body=node.body, type_ignores=[])))
            has_return = any(isinstance(n, ast.Return) for n in ast.walk(ast.Module(body=node.body, type_ignores=[])))
            if not has_raise and not has_return:
                issues.append((
                    node.lineno,
                    "`except Exception` without re-raise or return — consider using specific exception types",
                ))

    return issues


def scan_directory(root: str) -> int:
    total_issues = 0
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            issues = check_file(path)
            for lineno, msg in issues:
                print(f"{path}:{lineno}: {msg}")
                total_issues += 1
    return total_issues


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    n = scan_directory(root)
    if n:
        print(f"\n{n} dangerous exception pattern(s) found.")
        sys.exit(1)
    else:
        print("No dangerous exception patterns found.")
```

**`.pre-commit-config.yaml` integration:**
```yaml
- repo: local
  hooks:
    - id: check-exceptions
      name: Check dangerous exception patterns
      entry: python scripts/check_exceptions.py src/
      language: python
      types: [python]
```

**Expected Token Savings:** CI gate blocks bare-except patterns before they ship — prevents the entire class of silent-failure incidents where agents burn thousands of tokens returning empty results.

**Environment:** Any Python agent codebase; stdlib AST only, no dependencies.

---

## Comparison

| Option | Approach | Catches at | Prevents Silent Failure | Complexity |
|--------|---------|-----------|------------------------|------------|
| 1. Specific exception types | Code pattern | Runtime | Yes — propagates unknowns | Very Low |
| 2. Recoverable/fatal tiers | Code pattern | Runtime | Yes — fatal escalates | Low |
| 3. Structured error context | Always re-raise | Runtime | Yes — full context logged | Low |
| 4. `@catches` decorator | Explicit allow-list | Runtime | Yes — allow-list enforced | Low |
| 5. Global exception hooks | Infrastructure | Runtime (background) | Yes — nothing dropped | Medium |
| 6. Static analysis CI gate | Pre-commit / CI | Pre-deployment | Yes — prevented at source | Medium |

**Recommended path:** Apply Option 1 (specific exception types) to all existing `except Exception` blocks — it's the single most impactful fix. Add Option 6 (static analysis) to CI to prevent regressions. Use Option 5 (global hooks) for asyncio agents where background task exceptions are the most common silent-failure vector.
