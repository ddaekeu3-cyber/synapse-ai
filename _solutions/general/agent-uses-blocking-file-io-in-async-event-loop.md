---
layout: solution
title: "Agent uses blocking file I/O in async event loop"
category: general
description: "Async agent calls open(), read(), write() or json.load() directly in a coroutine. These synchronous calls block the entire event loop — all other coroutines stall while the disk operation completes. A 50ms file read blocks 50ms of concurrent API calls. Use asyncio.to_thread() or aiofiles to keep the loop running."
tags: [general, asyncio, blocking-io, file-io, performance, concurrency, aiofiles]
---

## Symptom

An async agent handles 10 concurrent user requests. One request triggers a `json.load(open("config.json"))` call inside a coroutine. The 5ms disk read blocks the event loop. During those 5ms, 9 other active coroutines — including ongoing streaming API responses — are frozen. With 100 file operations per minute, the event loop is effectively blocked for 500ms/min. Under load, P99 latency spikes and the event loop stalls during every file operation.

## Root Cause

Python's asyncio runs all coroutines on a single thread. `open()`, `read()`, `write()`, and `os.*` functions are synchronous — they block the OS thread until the I/O completes. Inside an `async def` function, calling these blocks the event loop because `await` only yields control at actual `await` points. A synchronous file call in a coroutine is a hidden blocking call that prevents the event loop from scheduling other coroutines.

## Fix

Use `asyncio.to_thread()` to run blocking file I/O in a thread pool executor, keeping the event loop free. For heavy file I/O, use `aiofiles` which provides a native async file API. For configuration files read at startup, read them once synchronously before the event loop starts.

---

### Option 1 — asyncio.to_thread() for blocking file operations

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


# WRONG — blocks the event loop:
# async def load_config():
#     with open("config.json") as f:
#         return json.load(f)   # ← blocks the event loop thread

# CORRECT — run blocking I/O in a thread pool:
async def load_config(path: str) -> dict:
    """Load a JSON file without blocking the event loop."""
    def _read():
        with open(path) as f:
            return json.load(f)

    return await asyncio.to_thread(_read)


async def save_output(path: str, data: dict) -> None:
    """Write JSON without blocking the event loop."""
    def _write():
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    await asyncio.to_thread(_write)


async def process_request(request_id: int, config: dict) -> dict:
    """Handle one request — can run concurrently with other requests."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Request {request_id}: summarize {config}"}],
    )
    result = {
        "request_id": request_id,
        "response": response.content[0].text,
    }
    # Save result without blocking
    await save_output(f"/tmp/result_{request_id}.json", result)
    return result


async def main():
    # Load config once — this is a startup operation, can be await'd
    try:
        config = await load_config("config.json")
    except FileNotFoundError:
        config = {"default": True}

    # Process 5 requests concurrently — file I/O doesn't block any of them
    results = await asyncio.gather(*[
        process_request(i, config) for i in range(5)
    ])
    print(f"Processed {len(results)} requests")


asyncio.run(main())
```

**Expected Token Savings:** Zero token change; `asyncio.to_thread()` keeps the event loop free — for 10 concurrent API calls each generating 100ms of streaming output, a 10ms file block causes 10 × 10ms = 100ms of additional latency per operation; total saved: seconds per minute under load.
**Environment:** Any async agent that reads/writes files; `asyncio.to_thread()` is the zero-dependency solution — no extra packages required.

---

### Option 2 — aiofiles for native async file I/O

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# pip install aiofiles
try:
    import aiofiles
    HAS_AIOFILES = True
except ImportError:
    HAS_AIOFILES = False
    print("aiofiles not installed — falling back to asyncio.to_thread")


async def read_file_async(path: str) -> str:
    """Read file asynchronously, choosing the best available method."""
    if HAS_AIOFILES:
        async with aiofiles.open(path, mode="r") as f:
            return await f.read()
    else:
        return await asyncio.to_thread(open(path).read)


async def write_file_async(path: str, content: str) -> None:
    """Write file asynchronously."""
    if HAS_AIOFILES:
        async with aiofiles.open(path, mode="w") as f:
            await f.write(content)
    else:
        await asyncio.to_thread(lambda: open(path, "w").write(content))


async def read_json_async(path: str) -> dict:
    text = await read_file_async(path)
    return json.loads(text)


async def write_json_async(path: str, data: dict) -> None:
    await write_file_async(path, json.dumps(data, indent=2))


async def run_agent_with_aiofiles(user_message: str, context_path: str) -> str:
    """Agent that reads context file and writes output without blocking."""

    # Read context file (non-blocking)
    try:
        context_data = await read_json_async(context_path)
        context_str = json.dumps(context_data, indent=2)[:1000]
    except FileNotFoundError:
        context_str = "No context file found."

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"{user_message}\n\nContext:\n{context_str}",
        }],
    )
    answer = response.content[0].text

    # Write output file (non-blocking)
    output = {"question": user_message, "answer": answer}
    await write_json_async("/tmp/agent_output.json", output)

    return answer


async def run_concurrent_agents():
    """Multiple agents run concurrently — none block each other on file I/O."""
    tasks = [
        run_agent_with_aiofiles(f"Question {i}", "/tmp/context.json")
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} concurrent agent runs")


import os
# Create a dummy context file for the demo
os.makedirs("/tmp", exist_ok=True)
with open("/tmp/context.json", "w") as f:
    json.dump({"project": "demo", "version": "1.0"}, f)

asyncio.run(run_concurrent_agents())
```

**Expected Token Savings:** Zero token change; `aiofiles` provides proper async I/O with OS-level async where available — for large files (>1MB), `aiofiles` outperforms `asyncio.to_thread()` because it uses true async I/O rather than a thread pool.
**Environment:** Agents with frequent or large file I/O; `aiofiles` is preferred over `asyncio.to_thread()` for files > 100KB or I/O rates > 100 ops/second.

---

### Option 3 — Startup vs runtime separation: sync reads before event loop starts

```python
import anthropic
import asyncio
import json
import os

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class AgentConfig:
    """
    Loads all config files synchronously at startup — before the event loop runs.
    Once loaded, all data is in memory; no file I/O needed during async execution.
    """
    def __init__(self, config_dir: str = "."):
        self.system_prompt: str = ""
        self.tool_schemas: list[dict] = []
        self.few_shot_examples: list[dict] = []
        self._load_all(config_dir)

    def _load_all(self, config_dir: str):
        """Load all config files synchronously at startup."""
        # System prompt
        prompt_path = os.path.join(config_dir, "system_prompt.txt")
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                self.system_prompt = f.read().strip()

        # Tool schemas
        tools_path = os.path.join(config_dir, "tools.json")
        if os.path.exists(tools_path):
            with open(tools_path) as f:
                self.tool_schemas = json.load(f)

        # Few-shot examples
        examples_path = os.path.join(config_dir, "examples.json")
        if os.path.exists(examples_path):
            with open(examples_path) as f:
                self.few_shot_examples = json.load(f)

        print(f"[Config] Loaded: system_prompt={len(self.system_prompt)} chars, "
              f"tools={len(self.tool_schemas)}, examples={len(self.few_shot_examples)}")


# Initialize synchronously at module level — no event loop blocking
config = AgentConfig()


async def run_agent(user_message: str) -> str:
    """
    Uses pre-loaded config — zero file I/O in the hot path.
    All config reads happened before asyncio.run() was called.
    """
    messages = []
    if config.few_shot_examples:
        messages.extend(config.few_shot_examples[:2])
    messages.append({"role": "user", "content": user_message})

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=config.system_prompt or "You are a helpful assistant.",
        tools=config.tool_schemas or [],
        messages=messages,
    )
    return response.content[0].text


async def main():
    # All file I/O already done — no blocking during concurrent execution
    results = await asyncio.gather(*[
        run_agent(f"Request {i}") for i in range(3)
    ])
    for r in results:
        print(r[:100])


asyncio.run(main())
```

**Expected Token Savings:** Zero token change; startup loading eliminates file I/O from the hot path entirely — the cleanest solution for configuration files that don't change at runtime; zero overhead per request.
**Environment:** Agents with static configuration files (system prompts, tool schemas, few-shot examples); startup loading is always preferred when the data doesn't change during the process lifetime.

---

### Option 4 — Async file cache with TTL refresh

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class CacheEntry:
    data: dict
    loaded_at: float
    path: str


class AsyncFileCache:
    """
    Caches file contents in memory with TTL-based refresh.
    Reads are served from memory; disk reads happen asynchronously only on miss/expiry.
    """
    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl = ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_json(self, path: str) -> dict:
        """Get JSON file content, using cache if fresh."""
        now = time.monotonic()
        entry = self._cache.get(path)

        if entry and (now - entry.loaded_at) < self.ttl:
            return entry.data   # cache hit — no I/O

        # Cache miss or expired — acquire lock to prevent stampede
        if path not in self._locks:
            self._locks[path] = asyncio.Lock()

        async with self._locks[path]:
            # Re-check after acquiring lock (another coroutine may have loaded it)
            entry = self._cache.get(path)
            if entry and (now - entry.loaded_at) < self.ttl:
                return entry.data

            # Load from disk in thread pool
            def _read():
                with open(path) as f:
                    return json.load(f)

            print(f"[FileCache] Loading: {path}")
            data = await asyncio.to_thread(_read)
            self._cache[path] = CacheEntry(data=data, loaded_at=time.monotonic(), path=path)
            return data


file_cache = AsyncFileCache(ttl_seconds=60.0)


async def run_agent_with_cache(user_message: str) -> str:
    """Agent reads config from cache — disk I/O happens at most once per TTL period."""
    try:
        # Will be loaded from disk once, then served from memory
        config = await file_cache.get_json("/tmp/agent_config.json")
        system = config.get("system_prompt", "You are a helpful assistant.")
    except FileNotFoundError:
        system = "You are a helpful assistant."

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def simulate_load():
    # 10 concurrent requests — file is loaded once, served from cache for the rest
    results = await asyncio.gather(*[
        run_agent_with_cache(f"Request {i}") for i in range(10)
    ])
    print(f"Completed {len(results)} requests with {len(file_cache._cache)} cache entries")


# Create test config
with open("/tmp/agent_config.json", "w") as f:
    json.dump({"system_prompt": "You are a helpful AI assistant."}, f)

asyncio.run(simulate_load())
```

**Expected Token Savings:** Zero token change; cache with lock prevents the "thundering herd" problem where 10 concurrent requests all miss the cache simultaneously and spawn 10 concurrent disk reads — the lock ensures only one disk read per TTL period.
**Environment:** High-concurrency agents reading config files; the async lock pattern is essential to prevent duplicate disk reads under concurrent load.

---

### Option 5 — Blocking I/O detector for development

```python
import anthropic
import asyncio
import functools
import time
import traceback
from typing import Callable

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Threshold for "blocking" (in seconds)
BLOCKING_THRESHOLD_MS = 5.0


class BlockingIODetector:
    """
    Development tool: patches the event loop to detect blocking calls.
    Logs a warning with a stack trace when the event loop is blocked > threshold.
    """
    def __init__(self, threshold_ms: float = 5.0):
        self.threshold = threshold_ms / 1000.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_check = time.monotonic()

    def install(self, loop: asyncio.AbstractEventLoop):
        """Install the detector on the event loop."""
        self._loop = loop
        original_run_until_complete = loop.run_until_complete

        def _slow_callback_duration():
            return self.threshold

        loop.slow_callback_duration = self.threshold
        loop.set_debug(True)   # enables slow callback warnings

        print(f"[BlockingDetector] Installed (threshold: {self.threshold*1000:.0f}ms)")


def detect_blocking_in_async(fn: Callable) -> Callable:
    """Decorator that measures how long a coroutine blocks the event loop."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = await fn(*args, **kwargs)
        elapsed = time.perf_counter() - start
        if elapsed > BLOCKING_THRESHOLD_MS / 1000.0:
            print(
                f"[BlockingWarning] {fn.__name__} took {elapsed*1000:.1f}ms "
                f"(threshold: {BLOCKING_THRESHOLD_MS}ms)"
            )
        return result
    return wrapper


# Demonstrate the problem vs the fix
@detect_blocking_in_async
async def bad_config_load() -> dict:
    """BAD: blocks the event loop."""
    import json
    with open("/tmp/agent_config.json") as f:
        return json.load(f)   # ← blocks here


@detect_blocking_in_async
async def good_config_load() -> dict:
    """GOOD: uses thread pool."""
    import json
    def _read():
        with open("/tmp/agent_config.json") as f:
            return json.load(f)
    return await asyncio.to_thread(_read)


async def demo_detection():
    import os, json
    os.makedirs("/tmp", exist_ok=True)
    with open("/tmp/agent_config.json", "w") as f:
        json.dump({"key": "value"}, f)

    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = BLOCKING_THRESHOLD_MS / 1000.0

    print("Testing bad config load (direct open):")
    await bad_config_load()

    print("\nTesting good config load (asyncio.to_thread):")
    await good_config_load()


asyncio.run(demo_detection())
```

**Expected Token Savings:** Zero token change; the detector catches blocking I/O during development before it reaches production — each undetected blocking call in production costs multiple users' request latency; early detection saves engineering time debugging latency spikes.
**Environment:** Development and staging environments; `loop.set_debug(True)` and `loop.slow_callback_duration` are built into Python's asyncio and require no extra packages.

---

### Option 6 — Migration helper: audit and fix all blocking I/O patterns

```python
import anthropic
import asyncio
import ast
import os

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Blocking I/O patterns that should be replaced in async code
BLOCKING_PATTERNS = {
    "open(": "Use: await asyncio.to_thread(lambda: open(...))",
    "os.read(": "Use: await asyncio.to_thread(os.read, ...)",
    "os.write(": "Use: await asyncio.to_thread(os.write, ...)",
    "os.listdir(": "Use: await asyncio.to_thread(os.listdir, ...)",
    "os.stat(": "Use: await asyncio.to_thread(os.stat, ...)",
    "json.load(": "Use: await asyncio.to_thread(json.load, ...)",
    "yaml.safe_load(": "Use: await asyncio.to_thread(yaml.safe_load, ...)",
    "pickle.load(": "Use: await asyncio.to_thread(pickle.load, ...)",
    "csv.reader(": "Use: await asyncio.to_thread(lambda: list(csv.reader(...)))",
    "pd.read_csv(": "Use: await asyncio.to_thread(pd.read_csv, ...)",
    "pd.read_json(": "Use: await asyncio.to_thread(pd.read_json, ...)",
}


def audit_file_for_blocking(filepath: str) -> list[dict]:
    """Scan a Python file for blocking I/O calls inside async functions."""
    issues = []
    try:
        with open(filepath) as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return issues

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return issues

    lines = source.splitlines()

    # Find async function bodies
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef,)):
            # Get all function call names within the async function
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    call_src = ast.unparse(child)
                    for pattern, fix in BLOCKING_PATTERNS.items():
                        if pattern.rstrip("(") in call_src:
                            lineno = child.lineno
                            line = lines[lineno - 1].strip() if lineno <= len(lines) else ""
                            issues.append({
                                "file": filepath,
                                "function": node.name,
                                "line": lineno,
                                "code": line,
                                "pattern": pattern,
                                "fix": fix,
                            })
    return issues


def audit_directory(directory: str) -> list[dict]:
    """Audit all Python files in a directory."""
    all_issues = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                issues = audit_file_for_blocking(path)
                all_issues.extend(issues)
    return all_issues


async def generate_fix(issue: dict) -> str:
    """Use Claude to suggest a fix for a detected blocking I/O issue."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Fix this blocking I/O call in an async function:\n"
                f"Code: {issue['code']}\n"
                f"Pattern: {issue['pattern']}\n"
                f"Suggested approach: {issue['fix']}\n"
                f"Show the corrected one-liner."
            ),
        }],
    )
    return response.content[0].text.strip()


# Comparison table
# | Option | Method | Dependencies | Best For |
# |--------|--------|-------------|----------|
# | 1 asyncio.to_thread | Thread pool | None | Simple one-off file ops |
# | 2 aiofiles | aiofiles package | aiofiles | Frequent large file I/O |
# | 3 Startup loading | Sync before loop | None | Static config files |
# | 4 Async cache | asyncio.Lock | None | Config with TTL refresh |
# | 5 Blocking detector | asyncio debug mode | None | Development debugging |
# | 6 Migration audit | ast.parse | None | Finding existing issues |

# Demo audit on current directory
issues = audit_directory(".")
if issues:
    print(f"Found {len(issues)} potential blocking I/O issues:")
    for issue in issues[:3]:
        print(f"  {issue['file']}:{issue['line']} in {issue['function']}()")
        print(f"    Code: {issue['code']}")
        print(f"    Fix: {issue['fix']}")
else:
    print("No blocking I/O patterns found — code is async-safe")
```

**Expected Token Savings:** Zero token change; AST-based audit finds all blocking I/O patterns in the codebase at once — one audit pass prevents dozens of future event loop blocks that would each cost users ~5–50ms of latency.
**Environment:** Existing async codebases being audited for blocking I/O; the audit tool generates a prioritized list of fixes, making it easy to address the highest-impact issues first.
