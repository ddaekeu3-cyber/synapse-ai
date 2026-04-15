---
layout: solution
title: "Agent Uses Hardcoded Timeout Values Across All Operations"
category: config
description: "Agent uses the same 30-second timeout for every operation — too long for health checks, too short for large file uploads and complex reasoning tasks — causing unnecessary failures or invisible hangs."
tags: [config, timeout, reliability, production, performance]
---

## Symptom

A health check endpoint times out after 30 seconds because the agent uses the same default for every call. Large file processing jobs fail with `ReadTimeout` after 30 seconds even though they need 3 minutes to complete. Meanwhile, a simple ping call that should fail fast in 2 seconds hangs for the full 30 seconds before surfacing an error. All three cases share one magic number that fits none of them.

## Root Cause

Developers set a single global timeout (`timeout=30`) in the Anthropic client constructor or in `requests.get(url, timeout=30)` and apply it everywhere. Different operations have fundamentally different latency profiles: health checks should be fast, streaming large files needs minutes, simple classification needs seconds. A single value is always wrong for at least some operations in the set.

## Fix

### Option 1 — Per-operation timeout constants

```python
import anthropic

# Operation-specific timeouts — grouped by latency profile
class Timeouts:
    HEALTH_CHECK     =  5.0   # fail fast; probe must be snappy
    SIMPLE_CLASSIFY  = 10.0   # one-token output; should be near-instant
    STANDARD_TASK    = 30.0   # typical agentic turn
    LONG_REASONING   = 90.0   # extended thinking, complex multi-step
    BATCH_PROCESSING = 300.0  # bulk document processing
    STREAMING_UPLOAD = 600.0  # large file operations

# Use different clients for different latency profiles
_clients: dict[str, anthropic.Anthropic] = {}

def get_client(operation: str) -> anthropic.Anthropic:
    timeout = getattr(Timeouts, operation.upper(), Timeouts.STANDARD_TASK)
    if operation not in _clients:
        _clients[operation] = anthropic.Anthropic(
            timeout=timeout,
            max_retries=1 if timeout < 15 else 2,
        )
    return _clients[operation]

def health_check() -> bool:
    """Fast check — fail quickly if the API is unreachable."""
    try:
        client = get_client("health_check")
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        return True
    except Exception as e:
        print(f"[health] failed in <{Timeouts.HEALTH_CHECK}s: {e}")
        return False

def classify(text: str) -> str:
    client = get_client("simple_classify")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{"role": "user", "content": f"Classify (positive/negative/neutral): {text}"}],
    )
    return resp.content[0].text.strip()

def reason_deeply(problem: str) -> str:
    client = get_client("long_reasoning")
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        thinking={"type": "enabled", "budget_tokens": 8000},
        messages=[{"role": "user", "content": problem}],
    )
    for block in resp.content:
        if block.type == "text":
            return block.text
    return ""

print(f"[health] {health_check()}")
print(f"[classify] {classify('This product is amazing!')}")
```

**Expected Token Savings:** Fast timeouts on health checks and classification prevent slow-fail scenarios where tokens accumulate while waiting; appropriate long timeouts on reasoning tasks prevent premature failures that would require expensive retries.
**Environment:** Any production agent with mixed operation types; single Anthropic client can be configured per-operation.

---

### Option 2 — Environment-driven timeout configuration

```python
import os
import anthropic
from dataclasses import dataclass

@dataclass
class TimeoutConfig:
    default_s:          float
    health_check_s:     float
    classify_s:         float
    reasoning_s:        float
    batch_s:            float
    connect_s:          float

def load_timeout_config() -> TimeoutConfig:
    """Load timeouts from environment variables with sensible defaults per env."""
    env = os.environ.get("AGENT_ENV", "development")

    if env == "production":
        defaults = dict(default_s=30, health_check_s=5, classify_s=10,
                        reasoning_s=120, batch_s=600, connect_s=10)
    elif env == "staging":
        defaults = dict(default_s=20, health_check_s=5, classify_s=8,
                        reasoning_s=90, batch_s=300, connect_s=8)
    else:  # development
        defaults = dict(default_s=60, health_check_s=10, classify_s=20,
                        reasoning_s=300, batch_s=900, connect_s=15)

    return TimeoutConfig(
        default_s=      float(os.environ.get("TIMEOUT_DEFAULT_S",      defaults["default_s"])),
        health_check_s= float(os.environ.get("TIMEOUT_HEALTH_S",       defaults["health_check_s"])),
        classify_s=     float(os.environ.get("TIMEOUT_CLASSIFY_S",     defaults["classify_s"])),
        reasoning_s=    float(os.environ.get("TIMEOUT_REASONING_S",    defaults["reasoning_s"])),
        batch_s=        float(os.environ.get("TIMEOUT_BATCH_S",        defaults["batch_s"])),
        connect_s=      float(os.environ.get("TIMEOUT_CONNECT_S",      defaults["connect_s"])),
    )

cfg = load_timeout_config()
print(f"[timeouts] env={os.environ.get('AGENT_ENV','development')}")
print(f"  default={cfg.default_s}s health={cfg.health_check_s}s "
      f"classify={cfg.classify_s}s reasoning={cfg.reasoning_s}s")

# Per-operation clients using env-configured timeouts
classify_client = anthropic.Anthropic(timeout=cfg.classify_s,  max_retries=2)
reason_client   = anthropic.Anthropic(timeout=cfg.reasoning_s, max_retries=1)

def classify_fast(text: str) -> str:
    resp = classify_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{"role": "user", "content": f"Sentiment (pos/neg/neu): {text}"}],
    )
    return resp.content[0].text.strip()

def analyse(task: str) -> str:
    resp = reason_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": task}],
    )
    return resp.content[0].text

print(f"[classify] {classify_fast('The service was outstanding.')}")
print(f"[analyse] {analyse('What are the three main risks of microservices?')[:100]}")
```

**Expected Token Savings:** Production uses tighter timeouts than development — in dev, long timeouts help debugging; in prod, tight timeouts surface failures fast, preventing token accumulation on hung calls.
**Environment:** Multi-environment deployments (dev/staging/prod); CI/CD pipelines where timeout behaviour must differ by stage.

---

### Option 3 — asyncio.wait_for() with per-operation timeout

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# Timeout registry — one place to change all timeouts
OPERATION_TIMEOUTS = {
    "ping":           2.0,
    "classify":       8.0,
    "summarise":     20.0,
    "analyse":       60.0,
    "batch_item":    45.0,
}

async def call_with_timeout(operation: str, coro) -> dict:
    """Wrap any async operation with its registered timeout."""
    timeout = OPERATION_TIMEOUTS.get(operation, 30.0)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "ok", "result": result, "latency_ms": ms, "timeout_s": timeout}
    except asyncio.TimeoutError:
        ms = int((time.monotonic() - t0) * 1000)
        return {
            "status":     "timeout",
            "operation":  operation,
            "elapsed_ms": ms,
            "timeout_s":  timeout,
            "message":    f"Operation '{operation}' exceeded {timeout}s timeout.",
        }

async def ping() -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "."}],
    )
    return "ok"

async def classify(text: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{"role": "user", "content": f"Classify sentiment (pos/neg/neu): {text}"}],
    )
    return resp.content[0].text.strip()

async def summarise(doc: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarise in 2 sentences: {doc}"}],
    )
    return resp.content[0].text

async def main():
    results = await asyncio.gather(
        call_with_timeout("ping",     ping()),
        call_with_timeout("classify", classify("This framework is excellent!")),
        call_with_timeout("summarise", summarise("asyncio is Python's async I/O library. " * 10)),
    )
    for r in results:
        print(f"[{r.get('operation','?') if r['status']=='timeout' else 'ok'}] "
              f"status={r['status']} latency={r.get('latency_ms','N/A')}ms")

asyncio.run(main())
```

**Expected Token Savings:** `asyncio.wait_for()` cancels the coroutine immediately on timeout — no further tokens are consumed after the deadline; per-operation timeouts prevent one slow call from blocking the event loop.
**Environment:** Async FastAPI or aiohttp agents; any async pipeline with mixed operation latencies.

---

### Option 4 — httpx timeout object with granular connect/read/write control

```python
import anthropic
import httpx

# httpx.Timeout lets you set connect, read, write, and pool timeouts independently
def make_client(operation: str) -> anthropic.Anthropic:
    """Create a client with operation-specific httpx timeout settings."""
    if operation == "health_check":
        timeout = httpx.Timeout(
            connect=2.0,   # fail fast on connection
            read=3.0,      # small response expected
            write=2.0,
            pool=1.0,
        )
    elif operation == "classification":
        timeout = httpx.Timeout(
            connect=5.0,
            read=10.0,     # fast model, fast response
            write=3.0,
            pool=5.0,
        )
    elif operation == "batch_processing":
        timeout = httpx.Timeout(
            connect=10.0,
            read=300.0,    # may need several minutes
            write=30.0,    # large payload upload
            pool=10.0,
        )
    else:  # standard
        timeout = httpx.Timeout(
            connect=10.0,
            read=60.0,
            write=10.0,
            pool=5.0,
        )

    return anthropic.Anthropic(
        http_client=httpx.Client(timeout=timeout),
    )

# Create operation-specific clients
health_client = make_client("health_check")
classify_client = make_client("classification")
batch_client  = make_client("batch_processing")

def health_check() -> bool:
    try:
        resp = health_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        return True
    except Exception:
        return False

def classify(text: str) -> str:
    resp = classify_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{"role": "user", "content": f"Positive/negative/neutral: {text}"}],
    )
    return resp.content[0].text.strip()

def process_batch_item(item: str) -> str:
    resp = batch_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": f"Process: {item}"}],
    )
    return resp.content[0].text

print(f"[health] {health_check()}")
print(f"[classify] {classify('The delivery was late and damaged.')}")
```

**Expected Token Savings:** Separate connect timeout (catches network outages fast) vs read timeout (allows slow but progressing responses to finish) prevents the common failure where a fast connection to a slow response causes premature cancellation.
**Environment:** Agents using the Anthropic SDK with httpx; fine-grained control needed for network-sensitive environments (high-latency links, VPNs, cross-region calls).

---

### Option 5 — Adaptive timeout: extend deadline for in-progress streaming

```python
import anthropic
import time
import threading

client = anthropic.Anthropic()

class AdaptiveStreamTimeout:
    """
    Start with a strict initial timeout; extend it automatically
    while tokens are still arriving (streaming is making progress).
    """

    def __init__(
        self,
        initial_timeout: float = 10.0,  # time to first token
        between_token_timeout: float = 5.0,  # max gap between tokens
    ):
        self.initial_timeout       = initial_timeout
        self.between_token_timeout = between_token_timeout
        self._last_token_at:  float = 0
        self._timed_out:       bool = False
        self._result:          list = []

    def stream(self, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
        self._last_token_at = time.monotonic()
        self._timed_out     = False

        def watchdog():
            while not self._result and not self._timed_out:
                now = time.monotonic()
                if now - self._last_token_at > self.between_token_timeout:
                    print(f"[timeout] no token for {self.between_token_timeout}s — aborting")
                    self._timed_out = True
                    return
                time.sleep(0.1)

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()

        text = ""
        try:
            with client.messages.stream(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for chunk in stream.text_stream:
                    if self._timed_out:
                        break
                    self._last_token_at = time.monotonic()
                    text += chunk
        except Exception as e:
            print(f"[stream] error: {e}")

        self._result.append(text)
        return text

streamer = AdaptiveStreamTimeout(initial_timeout=10.0, between_token_timeout=5.0)
result = streamer.stream("Explain the benefits of adaptive timeouts in distributed systems.")
print(f"[adaptive] received {len(result)} chars: {result[:100]}")
```

**Expected Token Savings:** Adaptive timeout allows slow-starting but progressing streams to complete (avoiding wasted partial tokens) while still aborting truly stuck calls; first-token timeout prevents indefinite waits on hung API connections.
**Environment:** Streaming-enabled agents; long reasoning tasks where progress is visible via token arrival rate.

---

### Option 6 — Timeout configuration file with runtime reload

```python
import json
import os
import time
import threading
import anthropic

TIMEOUT_CONFIG_FILE = "/tmp/agent_timeouts.json"

DEFAULT_TIMEOUTS = {
    "health_check":     5.0,
    "classification":  10.0,
    "summarisation":   30.0,
    "reasoning":       90.0,
    "batch_item":      60.0,
    "default":         30.0,
}

class ReloadableTimeoutConfig:
    """Load timeout config from file; reload when file changes (hot reload)."""

    def __init__(self, config_path: str):
        self._path        = config_path
        self._config      = dict(DEFAULT_TIMEOUTS)
        self._last_mtime  = 0.0
        self._lock        = threading.RLock()
        self._write_defaults()
        self._reload()

    def _write_defaults(self):
        if not os.path.exists(self._path):
            with open(self._path, "w") as f:
                json.dump(DEFAULT_TIMEOUTS, f, indent=2)

    def _reload(self):
        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return
            with open(self._path) as f:
                new_cfg = json.load(f)
            with self._lock:
                self._config = {**DEFAULT_TIMEOUTS, **new_cfg}
                self._last_mtime = mtime
            print(f"[timeouts] config reloaded from {self._path}")
        except Exception as e:
            print(f"[timeouts] reload failed: {e} — using cached config")

    def get(self, operation: str) -> float:
        self._reload()  # check for changes on every get
        with self._lock:
            return self._config.get(operation, self._config["default"])

timeout_cfg = ReloadableTimeoutConfig(TIMEOUT_CONFIG_FILE)

def get_client_for(operation: str) -> anthropic.Anthropic:
    timeout = timeout_cfg.get(operation)
    return anthropic.Anthropic(timeout=timeout)

def run_operation(operation: str, prompt: str) -> str:
    timeout = timeout_cfg.get(operation)
    print(f"[{operation}] using timeout={timeout}s")
    client = anthropic.Anthropic(timeout=timeout)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

# Demo: change timeouts at runtime by editing the JSON file
print(run_operation("classification", "Is this positive? 'Great product!'"))
print(run_operation("summarisation",  "Summarise: Python is a high-level programming language."))
print(f"\n[info] edit {TIMEOUT_CONFIG_FILE} to change timeouts without restarting the agent")
```

**Expected Token Savings:** Runtime-reloadable timeouts allow ops teams to adjust thresholds during incidents (e.g., tighten health check timeout when the API is degraded) without a code deploy; faster failure detection reduces token waste on hung calls.
**Environment:** Long-running production agents; on-call engineers who need to tune agent behaviour without deployment downtime.

---

## Comparison

| Option | Configuration Source | Hot Reload | Per-operation | Granularity | Best For |
|---|---|---|---|---|---|
| 1. Named constants | Code | No (redeploy) | Yes | Operation-level | Simple; readable; easy to audit |
| 2. Env vars per env | Environment | No | Yes | Per env + operation | Multi-stage deployments |
| 3. asyncio.wait_for() | Code constants | No | Yes | Operation-level | Async agents; event loop control |
| 4. httpx.Timeout object | Code | No | Yes | Connect/read/write | Fine-grained network timeout control |
| 5. Adaptive streaming | Runtime measurement | N/A | Streaming only | Token-gap based | Streaming agents; progress-aware |
| 6. Config file reload | JSON file | Yes | Yes | Operation-level | Ops teams; incident response |
