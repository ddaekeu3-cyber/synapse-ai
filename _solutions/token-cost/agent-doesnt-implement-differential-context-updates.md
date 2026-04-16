---
layout: solution
title: "Agent Doesn't Implement Differential Context Updates"
category: token-cost
description: "Send only the changed portion of context on each turn instead of the full accumulated history, reducing repeated token costs on long conversations."
tags: [token-cost, differential, context, delta, deduplication, compression]
---

# Agent Doesn't Implement Differential Context Updates

In long agent conversations, the same system context, tool schemas, and background documents are re-sent on every API call. If the context window grows by 200 tokens per turn but 2000 tokens are static, 90% of each call's input cost is redundant. Differential context updates send only what has changed since the last call — new messages, updated tool results, modified system fields — while using prompt caching or reference pointers for stable content.

## Option 1: Delta-Only Message Appending with Stable Prefix Cache

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a helpful assistant specializing in Python development.
You have deep knowledge of asyncio, type systems, and testing best practices.
Always provide runnable code examples when relevant."""

# Stable tool schema — cached with cache_control
TOOLS = [
    {
        "name": "run_code",
        "description": "Execute Python code and return the output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to run"},
                "timeout": {"type": "integer", "description": "Max execution seconds", "default": 10},
            },
            "required": ["code"],
        },
    }
]

# Mark stable content for prompt caching
CACHED_SYSTEM = [
    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
]


class DifferentialConversation:
    """Only sends new turns; stable prefix is cached server-side."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.total_input_tokens = 0
        self.cached_tokens = 0

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: list) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def ask(self, user_message: str) -> str:
        self.add_user(user_message)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=CACHED_SYSTEM,
            tools=TOOLS,
            messages=self.messages,
        )
        self.add_assistant(response.content)

        # Track token savings from caching
        usage = response.usage
        self.total_input_tokens += usage.input_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        self.cached_tokens += cache_read

        answer = next((b.text for b in response.content if hasattr(b, "text")), "")
        print(f"[tokens] input={usage.input_tokens} cached={cache_read} output={usage.output_tokens}")
        return answer


conv = DifferentialConversation()

# Each turn only sends new messages; system + tools are cached after turn 1
turns = [
    "What's the difference between asyncio.gather and asyncio.wait?",
    "Can you show a code example using asyncio.wait with error handling?",
    "How does timeout handling differ between the two?",
]

for turn in turns:
    print(f"\nUser: {turn[:60]}")
    answer = conv.ask(turn)
    print(f"Assistant: {answer[:150]}")

print(f"\nTotal input tokens: {conv.total_input_tokens}")
print(f"Total cached tokens: {conv.cached_tokens}")

# Expected Token Savings: 50-80% on turn 2+ for long system prompts; cache hits charged at ~10% of base rate
# Environment: Python 3.11+; cache_control ephemeral caches for ~5 minutes; works with claude-haiku and sonnet
```

## Option 2: Context Fingerprint with Change Detection

```python
import anthropic
import hashlib
import json
from typing import Any

client = anthropic.Anthropic()


def fingerprint(data: Any) -> str:
    """Stable hash of any JSON-serializable value."""
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()[:8]


class FingerprintedContext:
    """Tracks which context sections changed between calls."""

    def __init__(self) -> None:
        self._sections: dict[str, tuple[str, Any]] = {}  # name -> (hash, value)
        self.messages: list[dict] = []
        self.total_tokens = 0

    def set_section(self, name: str, value: Any) -> bool:
        """Update a context section. Returns True if changed."""
        fp = fingerprint(value)
        if name in self._sections and self._sections[name][0] == fp:
            return False  # No change
        self._sections[name] = (fp, value)
        return True

    def build_system(self) -> str:
        """Assemble current system prompt from all sections."""
        return "\n\n".join(
            f"=== {name} ===\n{value}"
            for name, (_, value) in self._sections.items()
        )

    def ask(self, user_message: str, changed_sections: list[str] | None = None) -> str:
        self.messages.append({"role": "user", "content": user_message})

        if changed_sections:
            print(f"[delta] Sections changed: {changed_sections}")
        else:
            print("[delta] No section changes — only new message sent")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self.build_system(),
            messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": response.content})
        self.total_tokens += response.usage.input_tokens

        return next((b.text for b in response.content if hasattr(b, "text")), "")


ctx = FingerprintedContext()

# Initial setup
ctx.set_section("role", "You are a Python expert assistant.")
ctx.set_section("guidelines", "Always use type hints. Prefer asyncio for I/O-bound tasks.")
ctx.set_section("user_profile", "Senior developer, prefers concise explanations.")

print("Turn 1 — all sections new:")
r1 = ctx.ask("What is a context manager?")
print(f"Answer: {r1[:100]}\n")

# Turn 2 — only user_profile changes
changed = []
if ctx.set_section("user_profile", "Senior developer, working on a FastAPI project now."):
    changed.append("user_profile")

print("Turn 2 — user_profile updated:")
r2 = ctx.ask("How do I use async context managers in FastAPI?", changed_sections=changed)
print(f"Answer: {r2[:100]}\n")

# Turn 3 — nothing changes
changed3 = []
print("Turn 3 — no changes:")
r3 = ctx.ask("Can you show an example with databases?", changed_sections=changed3)
print(f"Answer: {r3[:100]}")

print(f"\nTotal input tokens across 3 turns: {ctx.total_tokens}")

# Expected Token Savings: 20-40% when context sections are stable across turns; full savings accrue on section-heavy prompts
# Environment: Python 3.11+; fingerprint detects any content change, including whitespace — normalize before hashing
```

## Option 3: Sliding Window with Summarized History Prefix

```python
import anthropic

client = anthropic.Anthropic()

WINDOW_SIZE = 4       # Number of recent messages to keep verbatim
SUMMARY_THRESHOLD = 8  # Summarize when history exceeds this


def summarize_history(messages: list[dict]) -> str:
    """Compress old messages into a summary using the model."""
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[tool use]'}"
        for m in messages
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Summarize this conversation history in 3-5 bullet points:\n\n{history_text}"
        }],
    )
    return response.content[0].text


class SlidingWindowConversation:
    """Keeps a short verbatim window + a growing summary of older turns."""

    def __init__(self, window_size: int = WINDOW_SIZE) -> None:
        self.window_size = window_size
        self.recent: list[dict] = []
        self.summary: str = ""
        self.total_turns = 0
        self.total_tokens = 0

    def _system(self) -> str:
        if self.summary:
            return f"You are a helpful assistant.\n\nConversation history summary:\n{self.summary}"
        return "You are a helpful assistant."

    def _maybe_compress(self) -> None:
        if len(self.recent) > SUMMARY_THRESHOLD:
            # Compress oldest messages into summary
            to_compress = self.recent[:-self.window_size]
            new_summary = summarize_history(to_compress)
            if self.summary:
                self.summary += f"\n\n[Later]\n{new_summary}"
            else:
                self.summary = new_summary
            self.recent = self.recent[-self.window_size:]
            print(f"[compression] Compressed {len(to_compress)} messages into summary")

    def ask(self, user_message: str) -> str:
        self.recent.append({"role": "user", "content": user_message})
        self._maybe_compress()

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self._system(),
            messages=self.recent,
        )
        reply = next((b.text for b in response.content if hasattr(b, "text")), "")
        self.recent.append({"role": "assistant", "content": reply})
        self.total_turns += 1
        self.total_tokens += response.usage.input_tokens

        print(f"[turn {self.total_turns}] window={len(self.recent)} msgs | input_tokens={response.usage.input_tokens}")
        return reply


conv = SlidingWindowConversation(window_size=4)
questions = [
    "What are Python generators?",
    "How do they differ from lists?",
    "Show me a generator for Fibonacci numbers.",
    "What is the yield from syntax?",
    "How do generators work with asyncio?",
    "Can I use generators for file streaming?",
]

for q in questions:
    print(f"\nUser: {q}")
    answer = conv.ask(q)
    print(f"Assistant: {answer[:120]}")

print(f"\nTotal tokens: {conv.total_tokens} across {conv.total_turns} turns")
print(f"Summary length: {len(conv.summary)} chars")

# Expected Token Savings: 40-60% on long conversations; window keeps recent messages verbatim, history is compressed
# Environment: Python 3.11+; tune WINDOW_SIZE (3-6) and SUMMARY_THRESHOLD (6-12) based on conversation depth
```

## Option 4: Tool Result Deduplication with Change-Only Resend

```python
import anthropic
import hashlib
import json
from typing import Any

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "get_metrics",
        "description": "Get current system metrics.",
        "input_schema": {
            "type": "object",
            "properties": {"component": {"type": "string"}},
            "required": ["component"],
        },
    }
]

# Simulated metric store — values only change occasionally
METRICS: dict[str, dict] = {
    "database": {"latency_ms": 12, "connections": 45, "queries_per_sec": 230},
    "cache":    {"hit_rate": 0.94, "memory_mb": 512, "evictions": 3},
    "api":      {"rps": 1200, "error_rate": 0.002, "p99_ms": 45},
}


def get_metrics(component: str) -> dict:
    return METRICS.get(component, {"error": "unknown component"})


def result_hash(result: Any) -> str:
    return hashlib.md5(json.dumps(result, sort_keys=True).encode()).hexdigest()[:8]


class DeduplicatingContext:
    """Caches tool results and only re-sends when values change."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.result_cache: dict[str, tuple[str, Any]] = {}  # key -> (hash, value)
        self.saved_tokens = 0

    def handle_tool_calls(self, tool_calls: list) -> list[dict]:
        results = []
        for block in tool_calls:
            if block.type != "tool_use":
                continue
            key = f"{block.name}:{json.dumps(block.input, sort_keys=True)}"
            actual = get_metrics(block.input["component"])
            h = result_hash(actual)

            if key in self.result_cache and self.result_cache[key][0] == h:
                # Result unchanged — send abbreviated form
                cached_val = self.result_cache[key][1]
                result_content = f"[unchanged from cache] {json.dumps(cached_val)}"
                saved = len(json.dumps(actual))
                self.saved_tokens += saved // 4  # rough token estimate
                print(f"[dedup] {block.name}({block.input['component']}) unchanged — saved ~{saved//4} tokens")
            else:
                result_content = json.dumps(actual)
                self.result_cache[key] = (h, actual)
                print(f"[new]   {block.name}({block.input['component']}) — sending full result")

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_content})

        return results

    def ask(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        while True:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return next((b.text for b in response.content if hasattr(b, "text")), "")

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            tool_results = self.handle_tool_calls(tool_calls)
            self.messages.append({"role": "user", "content": tool_results})


ctx = DeduplicatingContext()

# Turn 1 — all results are new
print("=== Turn 1 ===")
r1 = ctx.ask("Check database, cache, and API metrics.")
print(f"Answer: {r1[:150]}\n")

# Simulate one metric changing
METRICS["database"]["latency_ms"] = 85  # database got slow

# Turn 2 — only database changed
print("=== Turn 2 ===")
r2 = ctx.ask("Check all metrics again. Has anything changed?")
print(f"Answer: {r2[:150]}")

print(f"\nEstimated tokens saved via dedup: ~{ctx.saved_tokens}")

# Expected Token Savings: 30-60% on polling agents that repeatedly fetch stable tool results
# Environment: Python 3.11+; invalidate cache on any field change; TTL-based expiry prevents stale reads
```

## Option 5: Incremental Document Patch with Diff Markers

```python
import anthropic
import difflib

client = anthropic.Anthropic()


def compute_patch(old: str, new: str) -> str:
    """Return a unified diff patch between old and new text."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="previous", tofile="current", n=2))
    return "".join(diff) if diff else ""


def apply_patch(base: str, patch: str) -> str:
    """Apply a unified diff patch to base text (simplified line-based)."""
    if not patch:
        return base
    lines = base.splitlines()
    result = list(lines)
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            result.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            stripped = line[1:]
            if stripped in result:
                result.remove(stripped)
    return "\n".join(result)


class PatchingContextAgent:
    """Sends full document on first call, then only patches on changes."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.last_document: str = ""
        self.total_tokens = 0
        self.patch_tokens_saved = 0

    def update_document(self, new_document: str) -> str:
        """Return context update: full doc or patch."""
        if not self.last_document:
            self.last_document = new_document
            return f"[FULL DOCUMENT]\n{new_document}"

        patch = compute_patch(self.last_document, new_document)
        if not patch:
            return "[DOCUMENT UNCHANGED — no update needed]"

        saved = len(new_document) - len(patch)
        self.patch_tokens_saved += max(0, saved) // 4
        print(f"[patch] Sending {len(patch)} chars instead of {len(new_document)} chars (saved ~{max(0,saved)//4} tokens)")
        self.last_document = new_document
        return f"[DOCUMENT PATCH]\n{patch}"

    def ask(self, user_message: str, document: str | None = None) -> str:
        content = user_message
        if document is not None:
            doc_update = self.update_document(document)
            content = f"{doc_update}\n\n{user_message}"

        self.messages.append({"role": "user", "content": content})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": response.content})
        self.total_tokens += response.usage.input_tokens
        return next((b.text for b in response.content if hasattr(b, "text")), "")


doc_v1 = """# API Spec
## Endpoints
- GET /users — list users
- POST /users — create user
- GET /users/{id} — get user by ID"""

doc_v2 = """# API Spec
## Endpoints
- GET /users — list users
- POST /users — create user
- GET /users/{id} — get user by ID
- DELETE /users/{id} — delete user
- PATCH /users/{id} — update user"""  # Two new endpoints added

doc_v3 = doc_v2  # No change

agent = PatchingContextAgent()

print("=== Turn 1 (full document) ===")
r1 = agent.ask("Summarize the API endpoints.", document=doc_v1)
print(f"Answer: {r1[:150]}\n")

print("=== Turn 2 (patch: 2 new endpoints) ===")
r2 = agent.ask("What endpoints were added?", document=doc_v2)
print(f"Answer: {r2[:150]}\n")

print("=== Turn 3 (no change) ===")
r3 = agent.ask("Are there any DELETE endpoints?", document=doc_v3)
print(f"Answer: {r3[:150]}")

print(f"\nTotal tokens: {agent.total_tokens} | Estimated saved via patches: ~{agent.patch_tokens_saved}")

# Expected Token Savings: 40-70% on document-editing workflows where documents are large and changes are small
# Environment: Python 3.11+; send patch when len(patch) < len(full_doc) * 0.5; otherwise send full doc
```

## Option 6: Structured State Diff with Field-Level Change Tracking

```python
import asyncio
import anthropic
import json
from typing import Any

client = anthropic.AsyncAnthropic()


def deep_diff(old: dict, new: dict, path: str = "") -> dict[str, Any]:
    """Return only the fields that changed between old and new dicts."""
    changes: dict[str, Any] = {}
    all_keys = set(old) | set(new)
    for key in all_keys:
        full_path = f"{path}.{key}" if path else key
        if key not in old:
            changes[full_path] = {"added": new[key]}
        elif key not in new:
            changes[full_path] = {"removed": old[key]}
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            nested = deep_diff(old[key], new[key], full_path)
            changes.update(nested)
        elif old[key] != new[key]:
            changes[full_path] = {"from": old[key], "to": new[key]}
    return changes


class StateDiffAgent:
    """Sends full state on first turn; only changed fields on subsequent turns."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.last_state: dict = {}
        self.total_tokens = 0

    def format_state_update(self, new_state: dict) -> str:
        if not self.last_state:
            self.last_state = new_state
            return f"[FULL STATE]\n{json.dumps(new_state, indent=2)}"

        diff = deep_diff(self.last_state, new_state)
        self.last_state = new_state

        if not diff:
            return "[STATE UNCHANGED]"

        print(f"[diff] {len(diff)} field(s) changed: {list(diff.keys())}")
        return f"[STATE DELTA — {len(diff)} change(s)]\n{json.dumps(diff, indent=2)}"

    async def ask(self, question: str, state: dict | None = None) -> str:
        content = question
        if state is not None:
            state_update = self.format_state_update(state)
            content = f"{state_update}\n\nQuestion: {question}"

        self.messages.append({"role": "user", "content": content})
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": response.content})
        self.total_tokens += response.usage.input_tokens
        print(f"[tokens] input={response.usage.input_tokens}")
        return next((b.text for b in response.content if hasattr(b, "text")), "")


async def main() -> None:
    agent = StateDiffAgent()

    state_v1 = {
        "deployment": {"version": "1.2.3", "replicas": 3, "region": "us-east-1"},
        "health": {"status": "healthy", "uptime_hours": 120},
        "traffic": {"rps": 1000, "error_rate": 0.001},
    }

    state_v2 = {
        "deployment": {"version": "1.2.3", "replicas": 5, "region": "us-east-1"},  # replicas changed
        "health": {"status": "degraded", "uptime_hours": 121},                      # status changed
        "traffic": {"rps": 1000, "error_rate": 0.001},                             # unchanged
    }

    state_v3 = state_v2  # no change

    print("=== Turn 1 (full state) ===")
    r1 = await agent.ask("Describe the current deployment status.", state=state_v1)
    print(f"Answer: {r1[:150]}\n")

    print("=== Turn 2 (delta: replicas + health.status changed) ===")
    r2 = await agent.ask("What changed and should I be concerned?", state=state_v2)
    print(f"Answer: {r2[:150]}\n")

    print("=== Turn 3 (no change) ===")
    r3 = await agent.ask("Is the error rate still within bounds?", state=state_v3)
    print(f"Answer: {r3[:150]}")

    print(f"\nTotal input tokens: {agent.total_tokens}")


asyncio.run(main())

# Expected Token Savings: 50-75% on monitoring/ops agents where most state fields are stable across turns
# Environment: Python 3.11+; deep_diff works for nested dicts; extend to handle list diffs for array-heavy state
```

## Comparison

| Option | Delta Mechanism | Cache-Aware | Documents | State | Best For |
|--------|----------------|-------------|-----------|-------|----------|
| 1. Stable Prefix Cache | prompt_caching cache_control | Yes | No | No | Repeated system prompt + tools |
| 2. Fingerprint Sections | MD5 per section | No | No | No | Multi-section context with partial updates |
| 3. Sliding Window Summary | Summarize old history | No | No | No | Long multi-turn conversations |
| 4. Tool Result Dedup | Hash tool outputs | No | No | No | Polling agents with stable tool results |
| 5. Document Patch | unified diff | No | Yes | No | Document review/editing workflows |
| 6. State Field Diff | Deep dict diff | No | No | Yes | Monitoring/ops agents with structured state |
