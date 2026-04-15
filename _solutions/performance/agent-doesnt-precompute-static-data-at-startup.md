---
layout: solution
title: "Agent Doesn't Precompute Static Data at Startup"
category: performance
description: "Agent recompiles regex patterns, rebuilds lookup tables, re-parses prompt templates, and re-loads static assets on every request instead of computing them once at startup and reusing them across all calls."
tags: [performance, startup, precomputation, caching, regex, efficiency, latency]
---

## Symptom

Every request triggers `re.compile(pattern)` on the same 20-pattern list, JSON parsing of the same static config file, reconstruction of the same few-shot example strings, and re-initialisation of the same lookup dictionaries. Profiling shows 15-40ms of CPU work per request that has nothing to do with the LLM call itself. Under load, this overhead compounds — 100 concurrent requests each spending 30ms on regex compilation consumes 3 CPU-seconds per request batch.

## Root Cause

Static data is computed inside request handlers because that's where developers first write the code. When prototyping, the performance cost is invisible. In production, the same work is repeated millions of times. Python's `re.compile()` is not free — compiling 20 patterns takes ~2ms. Parsing a 10KB JSON file takes ~1ms. Building a 500-token prompt string by string concatenation takes ~0.5ms. None of this changes between requests, so all of it should happen exactly once.

## Fix

### Option 1 — Module-level precomputation of regex patterns and lookup tables

```python
import re
import time
import anthropic

# WRONG: recompiled on every call
def validate_and_extract_bad(text: str) -> dict:
    email_re   = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}")   # recompiled each call
    url_re     = re.compile(r"https?://\S+")
    phone_re   = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")
    ip_re      = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    return {
        "emails": email_re.findall(text),
        "urls":   url_re.findall(text),
        "phones": phone_re.findall(text),
        "ips":    ip_re.findall(text),
    }

# CORRECT: compiled once at module load
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}")
_URL_RE   = re.compile(r"https?://\S+")
_PHONE_RE = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")
_IP_RE    = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Also precompute static lookup tables
_HTTP_STATUS = {
    200: "OK", 201: "Created", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable",
}

def validate_and_extract_good(text: str) -> dict:
    return {
        "emails": _EMAIL_RE.findall(text),
        "urls":   _URL_RE.findall(text),
        "phones": _PHONE_RE.findall(text),
        "ips":    _IP_RE.findall(text),
    }

client = anthropic.Anthropic()

sample_text = "Contact alice@example.com or visit https://example.com — call 555-123-4567 from 192.168.1.1"

N = 1000
t0 = time.perf_counter()
for _ in range(N):
    validate_and_extract_bad(sample_text)
bad_ms = (time.perf_counter() - t0) * 1000
print(f"Recompiled each call: {bad_ms:.0f}ms for {N} calls ({bad_ms/N:.2f}ms/call)")

t0 = time.perf_counter()
for _ in range(N):
    validate_and_extract_good(sample_text)
good_ms = (time.perf_counter() - t0) * 1000
print(f"Pre-compiled once:    {good_ms:.0f}ms for {N} calls ({good_ms/N:.2f}ms/call)")
print(f"Speedup: {bad_ms/good_ms:.1f}x")

# Use in agent post-processing
def process_agent_response(question: str) -> dict:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": question}],
    )
    return validate_and_extract_good(r.content[0].text)

result = process_agent_response("Give me an example email, URL, and phone number.")
print(f"\nExtracted: {result}")
```

**Expected Token Savings:** No token reduction; pre-compiling 4 regex patterns saves ~2ms per request — at 1,000 requests/min this is 2 CPU-seconds/min saved with zero code logic change; the larger the pattern set, the greater the saving.
**Environment:** All agents with response post-processing; regex pre-compilation is the single easiest performance fix and requires only moving `re.compile()` calls from inside functions to module level.

---

### Option 2 — Precompute prompt templates with string substitution slots

```python
import string
import time
import anthropic

client = anthropic.Anthropic()

# WRONG: system prompt rebuilt from parts on every call
def build_system_bad(agent_name: str, today: str, user_tier: str) -> str:
    few_shot = "\n".join([
        "Q: What is 2+2? A: 4.",
        "Q: What is the capital of France? A: Paris.",
        "Q: What does HTTP stand for? A: HyperText Transfer Protocol.",
    ])
    capabilities = ", ".join(["web search", "file read", "calculations", "email"])
    return (
        f"You are {agent_name}, a helpful assistant.\n"
        f"Today is {today}.\n"
        f"User tier: {user_tier}.\n"
        f"Capabilities: {capabilities}.\n\n"
        f"Examples:\n{few_shot}"
    )

# CORRECT: precompute the static parts, use Template for variable parts
_FEW_SHOT = "\n".join([
    "Q: What is 2+2? A: 4.",
    "Q: What is the capital of France? A: Paris.",
    "Q: What does HTTP stand for? A: HyperText Transfer Protocol.",
])
_CAPABILITIES = ", ".join(["web search", "file read", "calculations", "email"])

# Template with only the variable slots
_SYSTEM_TEMPLATE = string.Template(
    "You are $agent_name, a helpful assistant.\n"
    "Today is $today.\n"
    f"User tier: $user_tier.\n"
    f"Capabilities: {_CAPABILITIES}.\n\n"   # static part pre-embedded
    f"Examples:\n{_FEW_SHOT}"               # static part pre-embedded
)

def build_system_good(agent_name: str, today: str, user_tier: str) -> str:
    return _SYSTEM_TEMPLATE.substitute(
        agent_name=agent_name, today=today, user_tier=user_tier
    )

import datetime

N = 10_000
today = datetime.date.today().isoformat()

t0 = time.perf_counter()
for _ in range(N):
    build_system_bad("Aria", today, "pro")
bad_ms = (time.perf_counter() - t0) * 1000

t0 = time.perf_counter()
for _ in range(N):
    build_system_good("Aria", today, "pro")
good_ms = (time.perf_counter() - t0) * 1000

print(f"Built from parts:      {bad_ms:.0f}ms for {N:,} calls ({bad_ms/N*1000:.1f}µs/call)")
print(f"Template substitution: {good_ms:.0f}ms for {N:,} calls ({good_ms/N*1000:.1f}µs/call)")
print(f"Speedup: {bad_ms/good_ms:.1f}x")

# Use in agent
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    system=build_system_good("Aria", today, "pro"),
    messages=[{"role": "user", "content": "What can you do?"}],
)
print(f"\nA: {r.content[0].text.strip()[:120]}")
```

**Expected Token Savings:** Template substitution is ~5× faster than string concatenation at scale; for agents building a 500-token system prompt on every call at 1,000 req/min, switching to templates saves 5-15ms/call = 5-15 CPU-seconds/min with zero semantic change.
**Environment:** Agents with parameterised system prompts (user tier, date, agent name); template precomputation is especially valuable in async agents where the prompt-building cost blocks the event loop.

---

### Option 3 — Preload static JSON config and few-shot examples at import time

```python
import json
import time
import pathlib
import anthropic

client = anthropic.Anthropic()

# Write a sample static config for demonstration
_CONFIG_PATH = pathlib.Path("/tmp/agent_static_config.json")
_CONFIG_PATH.write_text(json.dumps({
    "model":        "claude-haiku-4-5-20251001",
    "max_tokens":   256,
    "temperature":  0,
    "few_shots": [
        {"q": "What is REST?",   "a": "REST is an architectural style for APIs using HTTP methods."},
        {"q": "What is GraphQL?","a": "GraphQL is a query language for APIs with a single endpoint."},
    ],
    "stop_words":   ["STOP", "END", "DONE"],
    "categories":   ["billing", "technical", "account", "general"],
}))

# WRONG: parsed on every call
def get_config_bad() -> dict:
    with open(_CONFIG_PATH) as f:
        return json.load(f)   # disk read + JSON parse every call

# CORRECT: parsed once at module load
with open(_CONFIG_PATH) as _f:
    _CONFIG: dict = json.load(_f)

# Precompute derived values from config
_STOP_WORD_SET: frozenset = frozenset(w.lower() for w in _CONFIG["stop_words"])
_FEW_SHOT_BLOCK: str      = "\n".join(
    f"Q: {fs['q']}\nA: {fs['a']}" for fs in _CONFIG["few_shots"]
)
_SYSTEM: str = f"You are a helpful assistant.\n\nExamples:\n{_FEW_SHOT_BLOCK}"

N = 5_000
t0 = time.perf_counter()
for _ in range(N):
    get_config_bad()
bad_ms = (time.perf_counter() - t0) * 1000

t0 = time.perf_counter()
for _ in range(N):
    _ = _CONFIG   # O(1) reference
good_ms = (time.perf_counter() - t0) * 1000

print(f"File read per call:   {bad_ms:.0f}ms for {N:,} calls ({bad_ms/N:.2f}ms/call)")
print(f"Module-level cache:   {good_ms:.0f}ms for {N:,} calls ({good_ms/N:.3f}ms/call)")
print(f"Speedup: {bad_ms/good_ms:.0f}x")

def ask(question: str) -> str:
    r = client.messages.create(
        model=_CONFIG["model"],
        max_tokens=_CONFIG["max_tokens"],
        system=_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text

r = ask("What is REST?")
print(f"\nA: {r.strip()[:150]}")
```

**Expected Token Savings:** Eliminating per-call file reads saves 1-5ms of I/O per request plus the JSON parse cost; for 1,000 req/min, file-read elimination saves 1,000-5,000ms of I/O per minute — equivalent to running 1-5 fewer server cores.
**Environment:** All agents loading static configuration files; module-level config loading is the mandatory first step before any other performance optimisation.

---

### Option 4 — Precompute embedding vectors for static few-shot examples

```python
import time
import anthropic

client = anthropic.Anthropic()

# Few-shot examples used for retrieval-augmented prompting
FEW_SHOT_EXAMPLES = [
    {"input": "I was charged twice",       "output": "billing",   "category": "billing"},
    {"input": "The app crashes on launch", "output": "technical", "category": "technical"},
    {"input": "How do I export my data",   "output": "account",   "category": "account"},
    {"input": "I can't log in",            "output": "technical", "category": "technical"},
    {"input": "Invoice for last month",    "output": "billing",   "category": "billing"},
]

# Simplified keyword-based similarity (replace with real embeddings in production)
def keyword_embed(text: str) -> dict[str, int]:
    words = set(text.lower().split())
    return words

def similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

# WRONG: similarity computed fresh on every query
def find_examples_bad(query: str, k: int = 2) -> list[dict]:
    q_embed = keyword_embed(query)
    scored  = [(ex, similarity(q_embed, keyword_embed(ex["input"]))) for ex in FEW_SHOT_EXAMPLES]
    scored.sort(key=lambda x: -x[1])
    return [ex for ex, _ in scored[:k]]

# CORRECT: precompute example embeddings at startup
_EXAMPLE_EMBEDDINGS = [(ex, keyword_embed(ex["input"])) for ex in FEW_SHOT_EXAMPLES]

def find_examples_good(query: str, k: int = 2) -> list[dict]:
    q_embed = keyword_embed(query)
    scored  = [(ex, similarity(q_embed, emb)) for ex, emb in _EXAMPLE_EMBEDDINGS]
    scored.sort(key=lambda x: -x[1])
    return [ex for ex, _ in scored[:k]]

N = 10_000
query = "my payment was processed twice"

t0 = time.perf_counter()
for _ in range(N):
    find_examples_bad(query)
bad_ms = (time.perf_counter() - t0) * 1000

t0 = time.perf_counter()
for _ in range(N):
    find_examples_good(query)
good_ms = (time.perf_counter() - t0) * 1000

print(f"Recomputed embeddings: {bad_ms:.0f}ms for {N:,} calls ({bad_ms/N*1000:.1f}µs/call)")
print(f"Pre-computed embeddings:{good_ms:.0f}ms for {N:,} calls ({good_ms/N*1000:.1f}µs/call)")
print(f"Speedup: {bad_ms/good_ms:.1f}x")

# Use in agent
examples = find_examples_good(query)
few_shot_block = "\n".join(f"Input: {e['input']}\nCategory: {e['output']}" for e in examples)
system = f"Classify support tickets.\n\nExamples:\n{few_shot_block}"

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=16,
    system=system,
    messages=[{"role": "user", "content": "My credit card was charged twice this month."}],
)
print(f"\nCategory: {r.content[0].text.strip()}")
```

**Expected Token Savings:** Precomputing embeddings for 5 examples avoids re-embedding on every query; with real embedding API calls (~10ms each), precomputing saves 50ms per request — a 25% reduction in end-to-end latency for RAG-based classification agents.
**Environment:** Retrieval-augmented agents using few-shot examples; precomputing static example embeddings is mandatory before deploying any embedding-based retrieval system to production.

---

### Option 5 — Startup validation: verify precomputed assets are correct

```python
import re
import json
import time
import sys
import anthropic

client = anthropic.Anthropic()

class AgentAssets:
    """
    All static assets precomputed at startup.
    Raises on initialisation failure so the process fails fast
    rather than serving bad data.
    """

    def __init__(self) -> None:
        t0 = time.perf_counter()
        self._init_patterns()
        self._init_config()
        self._init_prompts()
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"[startup] assets initialised in {elapsed:.1f}ms")

    def _init_patterns(self) -> None:
        raw_patterns = {
            "email":  r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
            "url":    r"https?://\S+",
            "ip":     r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "uuid":   r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        }
        self.patterns: dict[str, re.Pattern] = {}
        for name, pat in raw_patterns.items():
            try:
                self.patterns[name] = re.compile(pat, re.IGNORECASE)
            except re.error as e:
                raise RuntimeError(f"Invalid regex '{name}': {e}") from e

    def _init_config(self) -> None:
        self.model      = "claude-haiku-4-5-20251001"
        self.max_tokens = 256
        self.categories = frozenset(["billing", "technical", "account", "general"])

    def _init_prompts(self) -> None:
        cats = ", ".join(sorted(self.categories))
        self.classify_system = (
            f"Classify the support ticket into one of: {cats}. "
            "Reply with the category name only."
        )

    def extract(self, text: str) -> dict:
        return {name: pat.findall(text) for name, pat in self.patterns.items()}

    def validate(self) -> None:
        """Run self-tests — called at startup before serving traffic."""
        assert self.patterns["email"].match("a@b.com"), "email pattern broken"
        assert not self.patterns["email"].match("not-an-email"), "email false positive"
        assert self.patterns["url"].match("https://example.com"), "url pattern broken"
        assert "billing" in self.categories, "categories missing billing"
        print("[startup] validation passed")

# Initialise once — raises if broken, preventing bad deploys
ASSETS = AgentAssets()
ASSETS.validate()

def classify(ticket: str) -> str:
    r = client.messages.create(
        model=ASSETS.model,
        max_tokens=16,
        system=ASSETS.classify_system,
        messages=[{"role": "user", "content": ticket}],
    )
    return r.content[0].text.strip()

tickets = [
    "I was charged twice for my subscription.",
    "The app crashes when I open it.",
    "How do I export my contacts?",
]
for t in tickets:
    cat = classify(t)
    extracted = ASSETS.extract(t)
    print(f"  [{cat}] {t[:50]} | extracted={extracted}")
```

**Expected Token Savings:** Startup validation ensures precomputed assets are correct before the first request arrives; a broken regex that is caught at startup costs 0 user-visible errors vs. a broken regex discovered in production that corrupts 10,000 responses before being noticed.
**Environment:** Production agents; startup validation of precomputed assets is a reliability practice that prevents silent correctness regressions when patterns or config files are updated.

---

### Option 6 — Async precomputation: warm caches concurrently during startup

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# Assets warmed asynchronously during startup
class AsyncAgentCache:
    def __init__(self) -> None:
        self._model_validated = False
        self._sample_latency  = 0.0
        self._ready           = asyncio.Event()

    async def warm(self) -> None:
        """Fire warmup tasks concurrently — does not block server startup."""
        t0    = time.monotonic()
        tasks = [
            self._warm_model_connection(),
            self._precompute_static_data(),
        ]
        await asyncio.gather(*tasks)
        elapsed = (time.monotonic() - t0) * 1000
        self._ready.set()
        print(f"[warmup] complete in {elapsed:.0f}ms — model_ok={self._model_validated}")

    async def _warm_model_connection(self) -> None:
        t0 = time.monotonic()
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            self._model_validated = True
            self._sample_latency  = (time.monotonic() - t0) * 1000
            print(f"  [warmup] model validated in {self._sample_latency:.0f}ms")
        except Exception as e:
            print(f"  [warmup] model validation failed: {e}")

    async def _precompute_static_data(self) -> None:
        import re
        # Simulate precomputing expensive static data
        await asyncio.sleep(0)   # yield so other tasks can run
        self._patterns = {
            "email": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
            "url":   re.compile(r"https?://\S+"),
        }
        print("  [warmup] regex patterns compiled")

    async def ensure_ready(self) -> None:
        await self._ready.wait()

    def extract(self, text: str) -> dict:
        return {name: pat.findall(text) for name, pat in self._patterns.items()}

CACHE = AsyncAgentCache()

async def main() -> None:
    # Start warmup without blocking
    warmup_task = asyncio.create_task(CACHE.warm())

    # Simulate server accepting connections during warmup
    print("[server] accepting connections (warmup in background)...")

    # Wait for warmup before processing real requests
    await CACHE.ensure_ready()

    print("\n[server] ready — processing requests:")
    questions = ["Name an example email address.", "Name an example URL."]
    results   = await asyncio.gather(*[
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": q}],
        )
        for q in questions
    ])
    for q, r in zip(questions, results):
        text      = r.content[0].text
        extracted = CACHE.extract(text)
        print(f"  Q: {q}")
        print(f"  A: {text.strip()[:60]} | extracted={extracted}")

    await warmup_task

asyncio.run(main())
```

**Expected Token Savings:** Async warmup fires model validation and static data precomputation concurrently — both complete in max(individual_latency) instead of sum; for a 200ms model ping + 10ms static data, concurrent warmup completes in 200ms instead of 210ms, and the server can begin accepting connections before warmup completes.
**Environment:** Async FastAPI/aiohttp agents with a startup event; async warmup prevents cold-start latency on the first real request while keeping startup time to a minimum.

---

## Comparison

| Option | Type of Precomputation | When It Runs | Latency Saved Per Call | Best For |
|---|---|---|---|---|
| 1. Regex pre-compilation | Pattern objects | Module import | 0.5-5ms | All agents with regex post-processing |
| 2. Prompt templates | String Template | Module import | 0.1-2ms | Parameterised system prompts |
| 3. Static JSON config | Dict + derived sets | Module import | 1-5ms (I/O) | Config-driven agents |
| 4. Few-shot embeddings | Embedding vectors | Module import | 10-50ms (embedding) | RAG-based classification agents |
| 5. Startup validation | All assets + self-test | On process start | 0 (prevents errors) | Production — catch regressions early |
| 6. Async warmup | Connection + static data | Async startup event | 100-300ms (cold start) | Async agents with FastAPI lifespan |
