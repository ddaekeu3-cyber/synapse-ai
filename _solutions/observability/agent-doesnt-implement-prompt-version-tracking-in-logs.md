---
title: "Agent Doesn't Implement Prompt Version Tracking in Logs"
description: "When every LLM call logs which prompt version was used, engineers can correlate prompt changes with quality, cost, and latency regressions — without it, debugging production issues is guesswork."
difficulty: beginner
category: observability
tags: [observability, logging, prompt-management, versioning, debugging, cost-tracking]
---

# Agent Doesn't Implement Prompt Version Tracking in Logs

## Problem

Prompt changes are deployed frequently and silently. When quality drops, cost spikes, or latency increases after a deployment, engineers have no way to answer "which prompt version was running for these requests?" without either reading code history or adding expensive A/B frameworks. Even a basic version string in every log line would make root-cause analysis trivial.

**Symptoms:**
- Quality regression discovered 48 hours after a prompt change, with no correlation data
- Cost spike traced to a verbose prompt change only after manual log archaeology
- A/B test results are noisy because prompt version isn't a query filter
- Canary rollouts can't be safely monitored without per-request prompt version
- Rollback decisions are made on intuition rather than data

---

## Solution 1: Simple Version String in Every Log Record

Tag every API call log with a prompt version string derived from the prompt content hash.

```python
import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional
import anthropic

logger = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def compute_prompt_version(system_prompt: str, prefix: str = "v") -> str:
    """Stable short hash of prompt content — changes whenever prompt text changes."""
    digest = hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
    return f"{prefix}{digest}"


@dataclass
class VersionedPrompt:
    system: str
    version: str
    label: str  # Human-readable name, e.g. "customer-support-v3"

    @classmethod
    def from_text(cls, system: str, label: str) -> "VersionedPrompt":
        return cls(system=system, version=compute_prompt_version(system), label=label)


SYSTEM_PROMPT = VersionedPrompt.from_text(
    system="You are a concise, helpful assistant. Answer in 2-3 sentences maximum.",
    label="concise-assistant",
)


class VersionTrackedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(
        self,
        user_message: str,
        prompt: VersionedPrompt = SYSTEM_PROMPT,
        session_id: str = "",
    ) -> str:
        start = time.perf_counter()
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=prompt.system,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "llm_call "
            f"prompt_version={prompt.version} "
            f"prompt_label={prompt.label} "
            f"session={session_id} "
            f"input_tokens={response.usage.input_tokens} "
            f"output_tokens={response.usage.output_tokens} "
            f"latency_ms={latency_ms:.1f} "
            f"stop_reason={response.stop_reason}"
        )

        return response.content[0].text


async def demo():
    agent = VersionTrackedAgent(api_key="sk-...")
    for i in range(3):
        result = await agent.ask(
            f"What is machine learning? (question {i})",
            session_id=f"sess_{i}",
        )
        print(result[:80])

# asyncio.run(demo())
```

---

## Solution 2: Prompt Registry with Semantic Versioning

Maintain a central registry where prompts have explicit semver strings and metadata, enabling filtering and rollback.

```python
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic

logger = logging.getLogger("agent.prompts")


@dataclass
class PromptRecord:
    name: str
    version: str          # Semantic version: "2.1.0"
    system: str
    author: str = ""
    released_at: str = ""  # ISO-8601
    tags: list[str] = field(default_factory=list)

    def to_log_fields(self) -> dict:
        return {
            "prompt_name": self.name,
            "prompt_version": self.version,
            "prompt_author": self.author,
            "prompt_released_at": self.released_at,
        }


class PromptRegistry:
    def __init__(self):
        self._prompts: dict[str, PromptRecord] = {}

    def register(self, record: PromptRecord) -> None:
        key = f"{record.name}@{record.version}"
        self._prompts[key] = record
        logger.info(f"[registry] Registered prompt {key}")

    def get(self, name: str, version: str = "latest") -> PromptRecord:
        if version == "latest":
            matching = [r for k, r in self._prompts.items() if r.name == name]
            if not matching:
                raise KeyError(f"No prompt named '{name}'")
            # Return highest semver
            return sorted(matching, key=lambda r: tuple(int(x) for x in r.version.split(".")))[-1]
        key = f"{name}@{version}"
        if key not in self._prompts:
            raise KeyError(f"Prompt not found: {key}")
        return self._prompts[key]


REGISTRY = PromptRegistry()
REGISTRY.register(PromptRecord(
    name="customer-support",
    version="2.0.0",
    system="You are a friendly customer support agent. Always end with 'Is there anything else I can help you with?'",
    author="alice@acme.com",
    released_at="2026-03-01T00:00:00Z",
    tags=["production", "support"],
))
REGISTRY.register(PromptRecord(
    name="customer-support",
    version="2.1.0",
    system="You are a concise customer support agent. Solve problems in 2-3 sentences. Offer follow-up only if relevant.",
    author="bob@acme.com",
    released_at="2026-04-01T00:00:00Z",
    tags=["production", "support", "concise"],
))


class RegistryBackedAgent:
    def __init__(self, api_key: str, registry: PromptRegistry = REGISTRY):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.registry = registry

    async def respond(
        self,
        user_message: str,
        prompt_name: str = "customer-support",
        prompt_version: str = "latest",
        request_id: str = "",
    ) -> dict:
        prompt = self.registry.get(prompt_name, prompt_version)
        start = time.perf_counter()

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=prompt.system,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.perf_counter() - start) * 1000

        log_entry = {
            "event": "llm_response",
            "request_id": request_id,
            **prompt.to_log_fields(),
            "latency_ms": round(latency_ms, 1),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        logger.info(json.dumps(log_entry))

        return {
            "text": response.content[0].text,
            "prompt_version": prompt.version,
            "latency_ms": latency_ms,
        }


async def demo():
    agent = RegistryBackedAgent(api_key="sk-...")
    result = await agent.respond("My order hasn't arrived.", request_id="req_001")
    print(f"[{result['prompt_version']}] {result['text'][:80]}")

# asyncio.run(demo())
```

---

## Solution 3: Per-Request Prompt Fingerprint in Structured Logs

Emit structured JSON logs with a fingerprint field that captures both the prompt version and the model, enabling cross-dimensional analysis.

```python
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional
import anthropic


@dataclass
class LLMCallLog:
    event: str = "llm_call"
    request_id: str = ""
    session_id: str = ""
    user_id: str = ""
    model: str = ""
    prompt_name: str = ""
    prompt_version: str = ""
    prompt_fingerprint: str = ""   # hash(model + system_prompt)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    stop_reason: str = ""
    cost_usd: float = 0.0
    timestamp: float = 0.0

    def emit(self) -> None:
        record = asdict(self)
        print(json.dumps(record), file=sys.stdout)


def fingerprint(model: str, system: str) -> str:
    raw = f"{model}:{system}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# Pricing (per million tokens)
INPUT_PRICE = {"claude-opus-4-6": 3.0}
OUTPUT_PRICE = {"claude-opus-4-6": 15.0}


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price = INPUT_PRICE.get(model, 3.0) / 1_000_000
    out_price = OUTPUT_PRICE.get(model, 15.0) / 1_000_000
    return input_tokens * in_price + output_tokens * out_price


class FingerprintedAgent:
    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(
        self,
        messages: list[dict],
        system: str,
        prompt_name: str,
        prompt_version: str,
        request_id: str = "",
        session_id: str = "",
        user_id: str = "",
        max_tokens: int = 512,
    ) -> str:
        fp = fingerprint(self.model, system)
        start = time.perf_counter()

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        cost = compute_cost(self.model, response.usage.input_tokens, response.usage.output_tokens)

        LLMCallLog(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            model=self.model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_fingerprint=fp,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=round(latency_ms, 1),
            stop_reason=response.stop_reason,
            cost_usd=round(cost, 6),
            timestamp=time.time(),
        ).emit()

        return response.content[0].text


async def demo():
    agent = FingerprintedAgent(api_key="sk-...")
    reply = await agent.complete(
        messages=[{"role": "user", "content": "How do I reset my password?"}],
        system="You are a helpful support agent. Be brief and direct.",
        prompt_name="support-brief",
        prompt_version="1.3.2",
        request_id="req_abc",
        user_id="usr_999",
    )
    print(reply[:100])

# asyncio.run(demo())
```

---

## Solution 4: Prompt Diff Alerter — Log When Prompt Changes at Runtime

Compare the active prompt version to the previous version and emit a structured change event when a new version is deployed.

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional
import anthropic


@dataclass
class PromptChangeEvent:
    event: str = "prompt_version_changed"
    prompt_name: str = ""
    old_version: str = ""
    new_version: str = ""
    changed_at: float = 0.0

    def emit(self) -> None:
        print(json.dumps({
            "event": self.event,
            "prompt_name": self.prompt_name,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "changed_at": self.changed_at,
        }))


class LivePromptTracker:
    def __init__(self):
        self._active: dict[str, str] = {}  # name -> version hash

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:8]

    def track(self, name: str, system: str) -> str:
        current_hash = self._hash(system)
        prev = self._active.get(name)

        if prev is not None and prev != current_hash:
            PromptChangeEvent(
                prompt_name=name,
                old_version=prev,
                new_version=current_hash,
                changed_at=time.time(),
            ).emit()

        self._active[name] = current_hash
        return current_hash


tracker = LivePromptTracker()


class LiveTrackedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(
        self,
        user_message: str,
        system: str,
        prompt_name: str,
    ) -> str:
        version = tracker.track(prompt_name, system)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        print(json.dumps({
            "event": "llm_call",
            "prompt_name": prompt_name,
            "prompt_version": version,
            "output_tokens": response.usage.output_tokens,
        }))
        return response.content[0].text


async def demo():
    agent = LiveTrackedAgent(api_key="sk-...")
    system_v1 = "You are a helpful assistant."
    system_v2 = "You are an extremely concise assistant. Use bullet points only."

    await agent.ask("What is Python?", system_v1, "assistant")
    await agent.ask("What is Go?", system_v1, "assistant")  # Same version — no event
    await agent.ask("What is Rust?", system_v2, "assistant")  # Changed — emits change event

# asyncio.run(demo())
```

---

## Solution 5: Prompt Version Rollup Dashboard (In-Process)

Accumulate per-prompt-version stats in memory and expose a summary for dashboards or health endpoints.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse


@dataclass
class VersionStats:
    version: str
    call_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    error_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def record(self, input_tokens: int, output_tokens: int, latency_ms: float) -> None:
        self.call_count += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_latency_ms += latency_ms
        self.last_seen = time.time()

    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count else 0.0

    def avg_output_tokens(self) -> float:
        return self.total_output_tokens / self.call_count if self.call_count else 0.0

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "calls": self.call_count,
            "avg_latency_ms": round(self.avg_latency_ms(), 1),
            "avg_output_tokens": round(self.avg_output_tokens(), 1),
            "total_output_tokens": self.total_output_tokens,
            "errors": self.error_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


class PromptVersionDashboard:
    def __init__(self):
        self._stats: dict[str, dict[str, VersionStats]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def record(
        self,
        prompt_name: str,
        version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> None:
        async with self._lock:
            if version not in self._stats[prompt_name]:
                self._stats[prompt_name][version] = VersionStats(version=version)
            self._stats[prompt_name][version].record(input_tokens, output_tokens, latency_ms)

    async def summary(self) -> dict:
        async with self._lock:
            return {
                name: {v: stats.to_dict() for v, stats in versions.items()}
                for name, versions in self._stats.items()
            }


dashboard = PromptVersionDashboard()
app = FastAPI()


@app.get("/metrics/prompts")
async def prompt_metrics():
    return JSONResponse(await dashboard.summary())


class DashboardAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(self, message: str, system: str, prompt_name: str, version: str) -> str:
        start = time.perf_counter()
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        latency_ms = (time.perf_counter() - start) * 1000
        await dashboard.record(
            prompt_name, version,
            response.usage.input_tokens,
            response.usage.output_tokens,
            latency_ms,
        )
        return response.content[0].text
```

---

## Solution 6: Git-Integrated Prompt Version Tracking

Use the git commit hash of the file containing the prompt as the version, so every prompt change is automatically versioned and traceable.

```python
import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import anthropic


def git_file_commit(file_path: str) -> str:
    """Return the short SHA of the last commit that touched this file."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", file_path],
            capture_output=True, text=True, check=True,
        )
        sha = result.stdout.strip()
        return sha if sha else "untracked"
    except subprocess.CalledProcessError:
        return "unknown"


def git_current_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


@dataclass
class GitVersionedPrompt:
    name: str
    system: str
    source_file: str  # Path to the file containing this prompt

    @property
    def version(self) -> str:
        return git_file_commit(self.source_file)

    @property
    def deploy_version(self) -> str:
        return git_current_commit()


SUPPORT_PROMPT = GitVersionedPrompt(
    name="customer-support",
    system="You are a helpful customer support agent. Be concise and solution-focused.",
    source_file="prompts/customer_support.py",  # Track changes to this file
)


class GitVersionedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(
        self,
        message: str,
        prompt: GitVersionedPrompt = SUPPORT_PROMPT,
        request_id: str = "",
    ) -> dict:
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=prompt.system,
            messages=[{"role": "user", "content": message}],
        )

        log = {
            "request_id": request_id,
            "prompt_name": prompt.name,
            "prompt_file_version": prompt.version,   # Last git commit touching prompt file
            "deploy_version": prompt.deploy_version,  # Current HEAD
            "output_tokens": response.usage.output_tokens,
        }
        print(log)
        return {"text": response.content[0].text, **log}


async def demo():
    agent = GitVersionedAgent(api_key="sk-...")
    result = await agent.ask("How do I cancel my subscription?", request_id="req_1")
    print(result["text"][:80])

# asyncio.run(demo())
```

---

## Comparison

| Solution | Version Source | Structured Logs | Change Detection | Registry | Complexity |
|---|---|---|---|---|---|
| Hash of prompt text | SHA-256 of system string | No | No | No | Very Low |
| Semantic version registry | Explicit semver string | Yes | No | Yes | Low |
| Fingerprint in JSON logs | hash(model + system) | Yes | No | No | Low |
| Runtime change alerter | Hash comparison on each call | Yes | Yes | No | Low |
| In-process dashboard | Per-version stat rollup | Yes | No | No | Medium |
| Git commit hash | `git log` last-touch commit | Yes | Via git | No | Low |

**Recommendation:** Start with Solution 3 (fingerprint in JSON logs) — one extra field per log line, zero infrastructure. Move to Solution 2 (registry with semver) once you have multiple prompts and need controlled rollouts. Add Solution 4 (change alerter) to fire alerts in Slack/PagerDuty when a prompt version changes in production.
