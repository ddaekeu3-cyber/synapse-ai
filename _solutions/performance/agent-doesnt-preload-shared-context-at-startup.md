---
layout: solution
title: "Agent doesn't preload shared context at startup"
category: performance
description: "Agent re-fetches configuration files, documentation, and static reference data on every request. Each call pays the I/O and token cost of loading the same unchanged content, adding hundreds of milliseconds and thousands of tokens per request."
tags: [performance, startup, caching, preloading, token-cost, latency]
---

## Symptom

Each request begins with the agent reading a config file, loading a documentation corpus, or calling a reference API — even though the content never changes between requests. First-request latency is high, and `input_tokens` counts are inflated by the same static content repeated on every API call.

## Root Cause

Context loading code lives inside the request handler, not in a module-level or startup initializer. Every call to `run_agent()` rebuilds the system prompt from scratch, re-reads files, and re-fetches external data. There is no distinction between "content that changes per request" and "content that is stable for the lifetime of the process".

## Fix

Load stable content once at module import or process startup. Cache it in a module-level variable or a singleton. For content that changes infrequently (hourly, daily), add a TTL cache rather than per-request fetching.

---

### Option 1 — Module-level preload: load once at import time

```python
import anthropic
from pathlib import Path

# Loaded ONCE when the module is imported — not per request
_SYSTEM_PROMPT: str | None = None
_TOOL_DOCS: str | None = None


def _load_system_prompt() -> str:
    """Load system prompt from file. Called once at startup."""
    prompt_file = Path("system_prompt.txt")
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "You are a helpful assistant."


def _load_tool_docs() -> str:
    """Load tool documentation. Called once at startup."""
    docs_file = Path("tool_docs.md")
    if docs_file.exists():
        return docs_file.read_text(encoding="utf-8")
    return ""


# Initialize at import time
_SYSTEM_PROMPT = _load_system_prompt()
_TOOL_DOCS = _load_tool_docs()

client = anthropic.Anthropic(api_key="sk-live-...")


def get_system_prompt() -> str:
    """Returns the pre-loaded system prompt. No I/O on the hot path."""
    assert _SYSTEM_PROMPT is not None
    tool_section = f"\n\n## Tool Documentation\n{_TOOL_DOCS}" if _TOOL_DOCS else ""
    return _SYSTEM_PROMPT + tool_section


def run_agent(user_message: str) -> str:
    """Hot path: no file I/O, no setup work."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=get_system_prompt(),   # pre-loaded, no I/O
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None on tokens; eliminates per-request file I/O latency (typically 1–10 ms per file, but can be 100 ms+ on network-mounted filesystems).
**Environment:** Any agent deployed as a long-running process (web server, daemon); module-level initialization runs once on startup.

---

### Option 2 — Singleton with lazy initialization and thread safety

```python
import anthropic
import threading
import time
from pathlib import Path


class AgentContext:
    """Thread-safe singleton that loads shared context once."""

    _instance: "AgentContext | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "AgentContext":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            t0 = time.perf_counter()
            self.system_prompt = self._load("config/system_prompt.txt", "You are a helpful assistant.")
            self.knowledge_base = self._load("docs/knowledge_base.md", "")
            self.tool_catalog = self._load("config/tools.json", "{}")
            self.load_time_ms = round((time.perf_counter() - t0) * 1000)
            self._initialized = True
            print(f"AgentContext initialized in {self.load_time_ms}ms")

    @staticmethod
    def _load(path: str, default: str) -> str:
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.exists() else default

    @property
    def full_system_prompt(self) -> str:
        parts = [self.system_prompt]
        if self.knowledge_base:
            parts.append(f"\n\n## Knowledge Base\n{self.knowledge_base}")
        return "\n".join(parts)


# Initialize once at application startup
ctx = AgentContext()
ctx.initialize()

client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ctx.full_system_prompt,   # no I/O
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None on tokens; the singleton pattern is safe for multi-threaded servers (FastAPI, Flask with multiple workers per process) — each thread shares the loaded context.
**Environment:** Web servers with multiple threads per worker process; lazy init means startup isn't delayed if the module is imported in a context where it won't be used.

---

### Option 3 — TTL cache for content that changes infrequently

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CachedValue:
    value: str
    loaded_at: float
    ttl_seconds: float

    def is_stale(self) -> bool:
        return time.time() - self.loaded_at > self.ttl_seconds


class TTLContextCache:
    def __init__(self) -> None:
        self._cache: dict[str, CachedValue] = {}
        self._loaders: dict[str, tuple[Callable[[], str], float]] = {}

    def register(self, key: str, loader: Callable[[], str], ttl_seconds: float) -> None:
        """Register a content loader with a TTL."""
        self._loaders[key] = (loader, ttl_seconds)

    def get(self, key: str) -> str:
        entry = self._cache.get(key)
        if entry is None or entry.is_stale():
            loader, ttl = self._loaders[key]
            value = loader()
            self._cache[key] = CachedValue(value=value, loaded_at=time.time(), ttl_seconds=ttl)
            age = "new" if entry is None else "refreshed"
            print(f"Cache {age}: {key} (ttl={ttl}s)")
        return self._cache[key].value


def load_config() -> str:
    """Simulate loading config from a remote source."""
    return "Model: claude-sonnet-4-6\nMax retries: 3\nTimeout: 30s"


def load_knowledge_base() -> str:
    """Simulate loading a knowledge base that updates daily."""
    return "## Company FAQ\nQ: Hours? A: 9am–5pm EST."


def load_system_prompt() -> str:
    from pathlib import Path
    p = Path("system_prompt.txt")
    return p.read_text() if p.exists() else "You are a helpful assistant."


cache = TTLContextCache()
cache.register("config", load_config, ttl_seconds=300)         # refresh every 5 min
cache.register("knowledge_base", load_knowledge_base, ttl_seconds=3600)  # refresh hourly
cache.register("system_prompt", load_system_prompt, ttl_seconds=60)     # refresh every minute

client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent(user_message: str) -> str:
    system = (
        f"{cache.get('system_prompt')}\n\n"
        f"## Config\n{cache.get('config')}\n\n"
        f"{cache.get('knowledge_base')}"
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None on tokens; eliminates repeated I/O for semi-stable content while keeping it fresh; the TTL prevents stale configs from persisting indefinitely.
**Environment:** Agents where content updates on a known schedule (nightly DB refresh, hourly config push); tune TTL to match the update frequency.

---

### Option 4 — Async preload with `asyncio.gather` at startup

```python
import anthropic
import asyncio
import aiofiles
from pathlib import Path

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class AsyncContextLoader:
    def __init__(self) -> None:
        self.system_prompt: str = ""
        self.guidelines: str = ""
        self.examples: str = ""
        self._ready = asyncio.Event()

    async def load(self) -> None:
        """Load all context files concurrently at startup."""
        self.system_prompt, self.guidelines, self.examples = await asyncio.gather(
            self._read_file("config/system_prompt.txt", "You are a helpful assistant."),
            self._read_file("config/guidelines.md", ""),
            self._read_file("config/examples.md", ""),
        )
        self._ready.set()
        print(f"Context loaded: {sum(len(x) for x in [self.system_prompt, self.guidelines, self.examples])} chars")

    async def wait_ready(self) -> None:
        await self._ready.wait()

    @staticmethod
    async def _read_file(path: str, default: str) -> str:
        p = Path(path)
        if not p.exists():
            return default
        async with aiofiles.open(p, encoding="utf-8") as f:
            return await f.read()

    @property
    def full_system(self) -> str:
        parts = [self.system_prompt]
        if self.guidelines:
            parts.append(f"\n## Guidelines\n{self.guidelines}")
        if self.examples:
            parts.append(f"\n## Examples\n{self.examples}")
        return "\n".join(parts)


_loader = AsyncContextLoader()


async def startup() -> None:
    """Call this once when your async application starts."""
    await _loader.load()


async def run_agent_async(user_message: str) -> str:
    await _loader.wait_ready()   # no-op after startup completes
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_loader.full_system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def main() -> None:
    await startup()
    # All subsequent calls use pre-loaded context
    results = await asyncio.gather(
        run_agent_async("Question 1"),
        run_agent_async("Question 2"),
        run_agent_async("Question 3"),
    )
    for r in results:
        print(r)


asyncio.run(main())
```

**Expected Token Savings:** None on tokens; concurrent file loading at startup means no request is blocked waiting for I/O; the `asyncio.Event` ensures requests that arrive before loading completes wait correctly.
**Environment:** `asyncio`-based web servers (FastAPI, aiohttp); the parallel gather is meaningfully faster than sequential loading when multiple files are involved.

---

### Option 5 — Prompt-cache-aligned preload with stable prefix

```python
import anthropic
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

# Loaded once — this is the stable prefix that Anthropic will cache
_CACHED_PREFIX: str | None = None


def _build_stable_prefix() -> str:
    """
    Build the large, stable portion of the system prompt.
    This is the part we want Anthropic to cache across requests.
    """
    parts = ["You are a helpful assistant with access to the following resources:\n"]

    knowledge_file = Path("docs/knowledge_base.md")
    if knowledge_file.exists():
        parts.append(f"\n## Knowledge Base\n{knowledge_file.read_text()}\n")

    guidelines_file = Path("config/guidelines.md")
    if guidelines_file.exists():
        parts.append(f"\n## Guidelines\n{guidelines_file.read_text()}\n")

    return "".join(parts)


# Build the stable prefix once
_CACHED_PREFIX = _build_stable_prefix()


def run_agent(user_message: str, dynamic_context: str = "") -> str:
    # Stable prefix first (cacheable by Anthropic) + dynamic context at end
    system_parts = [_CACHED_PREFIX]
    if dynamic_context:
        system_parts.append(f"\n## Current Context\n{dynamic_context}")
    system = "".join(system_parts)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": user_message}],
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"Tokens: input={usage.input_tokens}, cache_read={cache_read}, cache_write={cache_write}")

    return response.content[0].text
```

**Expected Token Savings:** After the first request, the stable prefix is served from Anthropic's cache at ~10 % of full input token cost; a 5,000-token knowledge base costs 500 cached tokens on every subsequent request.
**Environment:** Agents with large static knowledge bases or guidelines; the module-level preload ensures the stable prefix is assembled once and the Anthropic cache is warmed on the first request.

---

### Option 6 — Versioned preload with hot reload on file change

```python
import anthropic
import hashlib
import time
from pathlib import Path
from threading import Thread, Lock


class HotReloadContext:
    """
    Preloads context at startup and watches for file changes.
    Reloads only when the content actually changes (hash-based).
    """

    def __init__(self, watch_files: list[str], poll_interval: float = 10.0) -> None:
        self._files = [Path(f) for f in watch_files]
        self._poll_interval = poll_interval
        self._lock = Lock()
        self._context: str = ""
        self._hashes: dict[str, str] = {}
        self._load_all()
        self._start_watcher()

    def _hash_file(self, path: Path) -> str:
        if not path.exists():
            return ""
        return hashlib.md5(path.read_bytes()).hexdigest()

    def _load_all(self) -> None:
        parts: list[str] = []
        new_hashes: dict[str, str] = {}
        for f in self._files:
            h = self._hash_file(f)
            new_hashes[str(f)] = h
            if f.exists():
                parts.append(f.read_text(encoding="utf-8"))
        with self._lock:
            self._context = "\n\n".join(parts)
            self._hashes = new_hashes
        print(f"Context loaded: {len(self._context)} chars")

    def _watch_loop(self) -> None:
        while True:
            time.sleep(self._poll_interval)
            changed = any(
                self._hash_file(f) != self._hashes.get(str(f), "")
                for f in self._files
            )
            if changed:
                print("Context files changed — reloading")
                self._load_all()

    def _start_watcher(self) -> None:
        t = Thread(target=self._watch_loop, daemon=True)
        t.start()

    @property
    def context(self) -> str:
        with self._lock:
            return self._context


ctx = HotReloadContext(
    watch_files=["config/system_prompt.txt", "docs/guidelines.md"],
    poll_interval=30.0,
)

client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ctx.context,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Comparison table
# | Option | Load Timing | Refresh | Thread Safe | Extra Dep |
# |--------|------------|---------|-------------|-----------|
# | 1 Module-level | Import time | Never | Yes (GIL) | None |
# | 2 Singleton lazy | First use | Never | Yes (Lock) | None |
# | 3 TTL cache | First use + TTL | On expiry | No (add Lock) | None |
# | 4 Async gather | Startup coroutine | Never | Yes (Event) | aiofiles |
# | 5 Cache-aligned | Import time | Never | Yes (GIL) | None |
# | 6 Hot reload | Import + watcher | On file change | Yes (Lock) | None |
```

**Expected Token Savings:** Hot reload picks up prompt updates without a process restart, preventing stale prompts that cause incorrect outputs requiring correction turns.
**Environment:** Production deployments where the system prompt is updated by non-engineers (PMs, content teams) who can edit a file without redeploying the service.
