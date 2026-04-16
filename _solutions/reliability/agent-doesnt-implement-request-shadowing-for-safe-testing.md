---
title: "Agent Doesn't Implement Request Shadowing for Safe Testing"
description: "AI agents deploy new model versions or prompt changes directly to production; without request shadowing, regressions only surface after users are already impacted."
category: reliability
difficulty: advanced
tags: [shadowing, canary, testing, a-b-testing, dark-launch, asyncio, fastapi]
---

# Agent Doesn't Implement Request Shadowing for Safe Testing

## Problem

Deploying a new model version or updated prompt to production without validation is a gamble. A/B testing requires splitting real user traffic, which exposes some users to the new version. Request shadowing (dark launch) mirrors production traffic to the new version asynchronously — users only see the primary response, but you collect real-world data on the shadow version's latency, cost, and correctness before promoting it.

## Solution 1: Fire-and-Forget Async Shadow with Response Comparison

Mirror every request to the shadow agent in the background; compare responses and log divergences.

```python
import asyncio
import time
import logging
import hashlib
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
primary_client = AsyncAnthropic()
shadow_client = AsyncAnthropic()

PRIMARY_MODEL = "claude-sonnet-4-6"
SHADOW_MODEL  = "claude-opus-4-6"   # candidate being tested

async def shadow_call(messages: list[dict], primary_response: str) -> None:
    """Run shadow call and compare to primary — never blocks caller."""
    t0 = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            shadow_client.messages.create(
                model=SHADOW_MODEL,
                max_tokens=1024,
                messages=messages,
            ),
            timeout=30.0,
        )
        shadow_text = resp.content[0].text
        latency_ms = (time.monotonic() - t0) * 1000

        # Simple divergence signal: normalized edit distance > 30%
        shorter = min(len(primary_response), len(shadow_text))
        longer  = max(len(primary_response), len(shadow_text))
        diverged = longer > 0 and (longer - shorter) / longer > 0.3

        logger.info(
            "shadow_response",
            extra={
                "shadow_model": SHADOW_MODEL,
                "latency_ms": round(latency_ms, 1),
                "primary_len": len(primary_response),
                "shadow_len": len(shadow_text),
                "diverged": diverged,
                "shadow_tokens": resp.usage.output_tokens,
            },
        )
    except asyncio.TimeoutError:
        logger.warning("shadow_timeout", extra={"model": SHADOW_MODEL})
    except Exception as e:
        logger.error("shadow_error", extra={"error": str(e)})

async def handle_request(messages: list[dict]) -> str:
    # Primary response — returned to user
    resp = await primary_client.messages.create(
        model=PRIMARY_MODEL,
        max_tokens=1024,
        messages=messages,
    )
    primary_text = resp.content[0].text

    # Shadow — fire and forget, never awaited by caller
    asyncio.create_task(shadow_call(messages, primary_text))

    return primary_text
```

**When to use**: Any model upgrade or significant prompt change. The shadow never touches the user response path.

---

## Solution 2: Configurable Shadow Rate with Traffic Sampling

Shadow only a fraction of traffic (e.g., 10%) to control shadow costs.

```python
import asyncio
import random
import time
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

class ShadowRouter:
    def __init__(
        self,
        primary_model: str,
        shadow_model: str,
        shadow_rate: float = 0.10,  # shadow 10% of requests
    ):
        self._primary = AsyncAnthropic()
        self._shadow = AsyncAnthropic()
        self._primary_model = primary_model
        self._shadow_model = shadow_model
        self._shadow_rate = shadow_rate
        self._stats = {"shadowed": 0, "diverged": 0, "shadow_errors": 0}

    async def call(self, messages: list[dict], **kwargs) -> str:
        # Always call primary
        resp = await self._primary.messages.create(
            model=self._primary_model,
            messages=messages,
            **kwargs,
        )
        primary_text = resp.content[0].text

        # Conditionally shadow
        if random.random() < self._shadow_rate:
            self._stats["shadowed"] += 1
            asyncio.create_task(self._shadow_and_compare(messages, primary_text, kwargs))

        return primary_text

    async def _shadow_and_compare(self, messages, primary_text, kwargs):
        try:
            t0 = time.monotonic()
            resp = await asyncio.wait_for(
                self._shadow.messages.create(
                    model=self._shadow_model,
                    messages=messages,
                    **kwargs,
                ),
                timeout=45.0,
            )
            shadow_text = resp.content[0].text
            latency_ms = (time.monotonic() - t0) * 1000

            # Mark diverged if responses differ meaningfully
            diverged = self._responses_differ(primary_text, shadow_text)
            if diverged:
                self._stats["diverged"] += 1

            logger.info(
                "shadow_sample",
                extra={
                    "shadow_model": self._shadow_model,
                    "latency_ms": round(latency_ms, 1),
                    "diverged": diverged,
                    "cost_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
                },
            )
        except Exception as e:
            self._stats["shadow_errors"] += 1
            logger.warning("shadow_sample_error", extra={"err": str(e)})

    def _responses_differ(self, a: str, b: str) -> bool:
        # Jaccard similarity on word sets
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return True
        jaccard = len(words_a & words_b) / len(words_a | words_b)
        return jaccard < 0.5

    @property
    def divergence_rate(self) -> float:
        if self._stats["shadowed"] == 0:
            return 0.0
        return self._stats["diverged"] / self._stats["shadowed"]

router = ShadowRouter(
    primary_model="claude-sonnet-4-6",
    shadow_model="claude-opus-4-6",
    shadow_rate=0.05,
)
```

**When to use**: Cost-sensitive shadow testing. Start at 1–5% traffic, ramp up as confidence grows.

---

## Solution 3: FastAPI Shadow Middleware

Intercept all requests at the middleware layer; forward shadows transparently without modifying handlers.

```python
import asyncio
import json
import time
import logging
import httpx
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

SHADOW_BACKEND = "http://shadow-agent:8001"
SHADOW_RATE = 0.20

class ShadowMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, shadow_url: str, rate: float = 0.20):
        super().__init__(app)
        self._shadow_url = shadow_url
        self._rate = rate
        self._http = httpx.AsyncClient(timeout=60.0)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Buffer request body (needed for both primary and shadow)
        body = await request.body()

        # Call primary handler
        t0 = time.monotonic()
        response = await call_next(request)
        primary_ms = (time.monotonic() - t0) * 1000

        # Reconstruct response body for logging (streaming-safe)
        resp_body = b""
        async for chunk in response.body_iterator:
            resp_body += chunk

        # Fire shadow asynchronously for sampled requests
        import random
        if (
            random.random() < self._rate
            and request.method == "POST"
            and "/agent" in request.url.path
        ):
            asyncio.create_task(
                self._shadow_request(
                    method=request.method,
                    path=str(request.url),
                    headers=dict(request.headers),
                    body=body,
                    primary_body=resp_body,
                    primary_ms=primary_ms,
                )
            )

        return Response(
            content=resp_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    async def _shadow_request(
        self, method, path, headers, body, primary_body, primary_ms
    ):
        shadow_url = self._shadow_url + path.split("//", 1)[-1].split("/", 1)[-1]
        t0 = time.monotonic()
        try:
            resp = await self._http.request(
                method=method,
                url=shadow_url,
                headers={k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")},
                content=body,
            )
            shadow_ms = (time.monotonic() - t0) * 1000
            logger.info(
                "shadow_request",
                extra={
                    "path": path,
                    "primary_ms": round(primary_ms, 1),
                    "shadow_ms": round(shadow_ms, 1),
                    "primary_status": 200,
                    "shadow_status": resp.status_code,
                    "shadow_size": len(resp.content),
                    "primary_size": len(primary_body),
                },
            )
        except Exception as e:
            logger.warning("shadow_middleware_error", extra={"err": str(e)})

app = FastAPI()
app.add_middleware(ShadowMiddleware, shadow_url=SHADOW_BACKEND, rate=SHADOW_RATE)
```

**When to use**: Testing a new shadow service deployment. Works independently of application logic.

---

## Solution 4: Shadow with Automated Regression Detection

Automatically flag the shadow as a regression if its error rate or latency exceeds thresholds.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

@dataclass
class ShadowMetrics:
    window: int = 100
    _latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    _errors: deque = field(default_factory=lambda: deque(maxlen=100))
    _divergences: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, latency_ms: float, error: bool, diverged: bool):
        self._latencies.append(latency_ms)
        self._errors.append(int(error))
        self._divergences.append(int(diverged))

    @property
    def p95_latency(self) -> float:
        if not self._latencies:
            return 0.0
        s = sorted(self._latencies)
        idx = int(len(s) * 0.95)
        return s[idx]

    @property
    def error_rate(self) -> float:
        return sum(self._errors) / max(len(self._errors), 1)

    @property
    def divergence_rate(self) -> float:
        return sum(self._divergences) / max(len(self._divergences), 1)

class RegressionDetectingShadow:
    def __init__(
        self,
        primary_p95_ms: float = 3000,
        max_extra_latency_ms: float = 2000,
        max_error_rate: float = 0.05,
        max_divergence_rate: float = 0.30,
    ):
        self._metrics = ShadowMetrics()
        self._primary_p95 = primary_p95_ms
        self._max_extra_latency = max_extra_latency_ms
        self._max_error_rate = max_error_rate
        self._max_divergence_rate = max_divergence_rate
        self._regression_detected = False

    def _check_regression(self):
        if self._regression_detected:
            return
        metrics = self._metrics
        if len(metrics._latencies) < 20:
            return  # Not enough data

        issues = []
        if metrics.p95_latency > self._primary_p95 + self._max_extra_latency:
            issues.append(f"p95_latency={metrics.p95_latency:.0f}ms exceeds threshold")
        if metrics.error_rate > self._max_error_rate:
            issues.append(f"error_rate={metrics.error_rate:.1%} exceeds threshold")
        if metrics.divergence_rate > self._max_divergence_rate:
            issues.append(f"divergence_rate={metrics.divergence_rate:.1%} exceeds threshold")

        if issues:
            self._regression_detected = True
            logger.error(
                "shadow_regression_detected",
                extra={"issues": issues, "action": "halt_shadow_promotion"},
            )

    async def run_shadow(self, fn, primary_response: str) -> None:
        t0 = time.monotonic()
        error, diverged = False, False
        try:
            shadow_resp = await asyncio.wait_for(fn(), timeout=30.0)
            latency_ms = (time.monotonic() - t0) * 1000
            diverged = self._differs(primary_response, shadow_resp)
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            error = True
            logger.warning("shadow_run_error", extra={"err": str(e)})

        self._metrics.record(latency_ms, error, diverged)
        self._check_regression()

    def _differs(self, a: str, b: str) -> bool:
        wa, wb = set(a.lower().split()), set(b.lower().split())
        if not wa or not wb:
            return True
        return len(wa & wb) / len(wa | wb) < 0.5

    @property
    def is_regression(self) -> bool:
        return self._regression_detected

shadow = RegressionDetectingShadow(primary_p95_ms=2000, max_divergence_rate=0.25)
```

**When to use**: Automated CI/CD pipelines where you want shadow testing to block promotion on detected regression.

---

## Solution 5: Prompt Shadow Test — Same Model, New Prompt

Shadow with the same model but a new system prompt to safely test prompt changes.

```python
import asyncio
import json
import logging
import time
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
client = AsyncAnthropic()

CURRENT_SYSTEM = """You are a helpful assistant. Be concise and accurate."""

CANDIDATE_SYSTEM = """You are an expert assistant. Always structure your response with:
1. Direct answer
2. Brief explanation
3. Example if applicable
Be thorough but concise."""

async def prompt_shadow_call(
    user_messages: list[dict],
    primary_response: str,
    candidate_system: str,
) -> None:
    t0 = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-sonnet-4-6",  # same model, different prompt
                max_tokens=1024,
                system=candidate_system,
                messages=user_messages,
            ),
            timeout=20.0,
        )
        shadow_text = resp.content[0].text
        latency_ms = (time.monotonic() - t0) * 1000

        # Score both responses on length, structure indicators
        primary_score = _quality_heuristic(primary_response)
        shadow_score  = _quality_heuristic(shadow_text)

        logger.info(
            "prompt_shadow",
            extra={
                "latency_ms": round(latency_ms, 1),
                "primary_tokens": len(primary_response.split()),
                "shadow_tokens": len(shadow_text.split()),
                "primary_score": primary_score,
                "shadow_score": shadow_score,
                "shadow_wins": shadow_score > primary_score,
            },
        )
    except Exception as e:
        logger.warning("prompt_shadow_error", extra={"err": str(e)})

def _quality_heuristic(text: str) -> float:
    """Simple proxy for response quality (replace with LLM judge in production)."""
    score = 0.0
    score += min(len(text.split()) / 100, 1.0) * 0.3    # length (up to 100 words ideal)
    score += 0.3 if any(c in text for c in ["1.", "2.", "-", "•"]) else 0.0  # structure
    score += 0.2 if "example" in text.lower() else 0.0   # has example
    score += 0.2 if text.endswith((".", "!", "?")) else 0.0  # complete sentence
    return round(score, 2)

async def handle_with_prompt_shadow(user_messages: list[dict]) -> str:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CURRENT_SYSTEM,
        messages=user_messages,
    )
    primary_text = resp.content[0].text
    asyncio.create_task(
        prompt_shadow_call(user_messages, primary_text, CANDIDATE_SYSTEM)
    )
    return primary_text
```

**When to use**: Prompt engineering iterations. Validate new prompts against production traffic before swapping.

---

## Solution 6: Shadow Replay from Traffic Recording

Record production requests and replay them against the shadow offline, enabling testing without live traffic.

```python
import asyncio
import json
import time
import logging
from pathlib import Path
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
client = AsyncAnthropic()

RECORDING_FILE = Path("/tmp/agent_traffic_recording.jsonl")

class TrafficRecorder:
    """Record production requests for later shadow replay."""

    def __init__(self, path: Path, sample_rate: float = 0.01):
        self._path = path
        self._rate = sample_rate
        self._fh = open(path, "a")

    def record(self, messages: list[dict], response: str, model: str):
        import random
        if random.random() > self._rate:
            return
        entry = json.dumps({
            "ts": time.time(),
            "model": model,
            "messages": messages,
            "primary_response": response,
        })
        self._fh.write(entry + "\n")
        self._fh.flush()

    def close(self):
        self._fh.close()

class ShadowReplayer:
    """Replay recorded traffic against a shadow model/prompt offline."""

    def __init__(self, recording_path: Path, shadow_model: str, concurrency: int = 5):
        self._path = recording_path
        self._shadow_model = shadow_model
        self._sem = asyncio.Semaphore(concurrency)
        self._results: list[dict] = []

    async def replay_one(self, entry: dict) -> dict:
        async with self._sem:
            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model=self._shadow_model,
                        max_tokens=1024,
                        messages=entry["messages"],
                    ),
                    timeout=30.0,
                )
                shadow_text = resp.content[0].text
                latency_ms = (time.monotonic() - t0) * 1000
                primary = entry["primary_response"]
                wa, wb = set(primary.lower().split()), set(shadow_text.lower().split())
                jaccard = len(wa & wb) / max(len(wa | wb), 1)
                return {
                    "ok": True,
                    "latency_ms": round(latency_ms, 1),
                    "diverged": jaccard < 0.5,
                    "jaccard": round(jaccard, 3),
                }
            except Exception as e:
                return {"ok": False, "error": str(e), "latency_ms": (time.monotonic()-t0)*1000}

    async def run(self) -> dict:
        entries = [json.loads(l) for l in self._path.read_text().splitlines() if l.strip()]
        logger.info(f"Replaying {len(entries)} recorded requests against {self._shadow_model}")
        results = await asyncio.gather(*[self.replay_one(e) for e in entries])
        ok = [r for r in results if r["ok"]]
        diverged = [r for r in ok if r.get("diverged")]
        return {
            "total": len(results),
            "ok": len(ok),
            "errors": len(results) - len(ok),
            "divergence_rate": len(diverged) / max(len(ok), 1),
            "p95_latency_ms": sorted(r["latency_ms"] for r in ok)[int(len(ok)*0.95)] if ok else 0,
        }

# Offline replay: python -c "asyncio.run(ShadowReplayer(RECORDING_FILE, 'claude-opus-4-6').run())"
```

**When to use**: Offline regression testing before any production traffic is shadowed. Record 1% of traffic continuously.

---

## Comparison

| Solution | User Impact | Cost | Regression Detection | Deployment Required | Best For |
|---|---|---|---|---|---|
| Fire-and-forget async | None | 100% shadow calls | Manual log review | No | Simple model upgrade testing |
| Sampled shadow rate | None | Configurable (1–20%) | Manual | No | Cost-sensitive shadow testing |
| Middleware shadow | None | Per-request overhead | Manual | New service | Full-stack HTTP shadow |
| Automated regression | None | Sampled | Automatic | No | CI/CD gate for promotion |
| Prompt shadow | None | 100% same-model | Heuristic score | No | Prompt A/B testing |
| Traffic replay | None | Offline only | Batch analysis | No | Pre-production validation |

**Rule of thumb**: Always shadow before promoting. Start at 1% traffic, run for 24 hours, check divergence rate. If < 10% divergence and latency within SLA, promote.
