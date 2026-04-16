---
title: "Agent Doesn't Implement Synthetic Monitoring for Agent Health"
description: "AI agents rely solely on error logs and passive metrics; without synthetic monitors proactively running real test flows, silent regressions and partial failures go undetected until users complain."
category: observability
difficulty: intermediate
tags: [synthetic-monitoring, canary, alerting, healthcheck, prometheus, asyncio, testing]
---

# Agent Doesn't Implement Synthetic Monitoring for Agent Health

## Problem

Passive monitoring (logs + metrics) only catches failures that produce visible errors. A subtler failure — the model returning degraded output, a tool silently timing out, embeddings returning stale results — may produce no error log at all. Synthetic monitoring runs real representative flows against your production or staging agent on a schedule, measures correctness and latency, and alerts before users notice.

## Solution 1: Periodic Canary Request with Latency and Correctness Check

Run a known-answer probe against the live agent every minute. Alert if the answer is wrong or latency spikes.

```python
import asyncio
import time
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# Canary probes: (prompt, expected_substring, max_latency_ms)
CANARY_PROBES = [
    ("What is 2 + 2?", "4", 5000),
    ("Say exactly: HEALTH_OK", "HEALTH_OK", 3000),
    ("List the first 3 prime numbers separated by commas.", "2, 3, 5", 6000),
]

client = AsyncAnthropic()

async def run_canary_probe(prompt: str, expected: str, max_ms: float) -> dict:
    t0 = time.monotonic()
    try:
        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=max_ms / 1000 + 1,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        text = msg.content[0].text.strip()
        correct = expected.lower() in text.lower()
        return {
            "ok": correct and latency_ms <= max_ms,
            "latency_ms": round(latency_ms, 1),
            "correct": correct,
            "latency_ok": latency_ms <= max_ms,
            "response": text[:100],
        }
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": max_ms, "correct": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def synthetic_monitor_loop(interval_seconds: float = 60.0):
    while True:
        results = await asyncio.gather(*[
            run_canary_probe(p, e, m) for p, e, m in CANARY_PROBES
        ])
        failed = [r for r in results if not r["ok"]]
        if failed:
            logger.error("synthetic_probe_failed", extra={"failed": failed, "total": len(results)})
        else:
            logger.info("synthetic_probe_passed", extra={"probes": len(results)})
        await asyncio.sleep(interval_seconds)

# Start alongside your agent service
async def main():
    asyncio.create_task(synthetic_monitor_loop(interval_seconds=60))
    # ... start your agent server
```

**When to use**: Any production agent. Use Haiku for canary probes to keep costs negligible.

---

## Solution 2: Tool-Flow Synthetic Test with End-to-End Validation

Simulate a complete multi-tool agent flow and validate every step's output, not just final response.

```python
import asyncio
import time
import json
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

@dataclass
class SyntheticFlowResult:
    flow_name: str
    ok: bool
    steps: list[dict] = field(default_factory=list)
    total_ms: float = 0.0
    error: str = ""

async def synthetic_tool_flow(flow_name: str = "search_and_summarize") -> SyntheticFlowResult:
    """Run a full tool-use flow synthetically and verify each step."""
    client = AsyncAnthropic()
    result = SyntheticFlowResult(flow_name=flow_name, ok=False)
    t0 = time.monotonic()

    tools = [
        {
            "name": "search",
            "description": "Search for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    messages = [{"role": "user", "content": "Search for 'synthetic monitoring' and summarize."}]

    try:
        # Step 1: Initial call — expect tool use
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=tools,
                messages=messages,
            ),
            timeout=10.0,
        )

        tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]
        step1 = {
            "name": "initial_call",
            "ok": len(tool_use_blocks) > 0,
            "tool_calls": len(tool_use_blocks),
        }
        result.steps.append(step1)
        if not step1["ok"]:
            result.error = "no_tool_call_in_step1"
            return result

        # Step 2: Inject tool result and get final answer
        tool_block = tool_use_blocks[0]
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": "Synthetic monitoring is the practice of running scripted tests against systems to detect failures proactively.",
            }],
        })

        resp2 = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=tools,
                messages=messages,
            ),
            timeout=10.0,
        )
        final_text = " ".join(b.text for b in resp2.content if hasattr(b, "text"))
        step2 = {
            "name": "final_answer",
            "ok": len(final_text) > 20 and "synthetic" in final_text.lower(),
            "response_length": len(final_text),
        }
        result.steps.append(step2)
        result.ok = all(s["ok"] for s in result.steps)

    except asyncio.TimeoutError:
        result.error = "timeout"
    except Exception as e:
        result.error = str(e)
    finally:
        result.total_ms = (time.monotonic() - t0) * 1000

    return result

async def run_flow_monitors():
    while True:
        res = await synthetic_tool_flow()
        if not res.ok:
            import logging
            logging.getLogger(__name__).error(
                "synthetic_flow_failed",
                extra={"flow": res.flow_name, "error": res.error, "steps": res.steps},
            )
        await asyncio.sleep(120)
```

**When to use**: Agents with tool-use workflows. Catches tool-routing regressions that simple ping probes miss.

---

## Solution 3: Prometheus Metrics for Synthetic Probe Results

Export synthetic probe latency and success rate as Prometheus metrics for Grafana dashboards and alerting.

```python
import asyncio
import time
from prometheus_client import Counter, Histogram, Gauge, start_http_server

synthetic_probe_total = Counter(
    "synthetic_probe_total",
    "Total synthetic probes run",
    ["probe_name", "status"],  # status: success | failure | timeout
)
synthetic_probe_latency = Histogram(
    "synthetic_probe_latency_seconds",
    "Latency of synthetic probes",
    ["probe_name"],
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0],
)
synthetic_probe_last_success = Gauge(
    "synthetic_probe_last_success_timestamp",
    "Unix timestamp of last successful probe",
    ["probe_name"],
)
synthetic_probe_correctness = Gauge(
    "synthetic_probe_correctness_ratio",
    "Rolling correctness ratio (last 10 probes)",
    ["probe_name"],
)

from collections import deque
_correctness_window: dict[str, deque] = {}

async def instrumented_probe(name: str, prompt: str, expected: str):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    t0 = time.monotonic()
    status = "failure"
    correct = False
    try:
        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=10.0,
        )
        text = msg.content[0].text
        correct = expected.lower() in text.lower()
        status = "success" if correct else "failure"
        if correct:
            synthetic_probe_last_success.labels(probe_name=name).set(time.time())
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception:
        status = "failure"
    finally:
        elapsed = time.monotonic() - t0
        synthetic_probe_total.labels(probe_name=name, status=status).inc()
        synthetic_probe_latency.labels(probe_name=name).observe(elapsed)

        window = _correctness_window.setdefault(name, deque(maxlen=10))
        window.append(1 if correct else 0)
        synthetic_probe_correctness.labels(probe_name=name).set(
            sum(window) / len(window)
        )

async def metrics_monitor_loop():
    start_http_server(9101)  # Prometheus scrape endpoint
    probes = [
        ("math_check", "What is 7 * 8?", "56"),
        ("health_ping", "Reply with only: PONG", "PONG"),
    ]
    while True:
        await asyncio.gather(*[instrumented_probe(n, p, e) for n, p, e in probes])
        await asyncio.sleep(60)
```

Grafana alert rule:
```yaml
- alert: SyntheticProbeCorrectnessLow
  expr: synthetic_probe_correctness_ratio < 0.8
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Agent correctness below 80% for probe {{ $labels.probe_name }}"
```

**When to use**: Any agent with a Prometheus/Grafana stack.

---

## Solution 4: Multi-Region Synthetic Probe Dispatcher

Run probes from multiple geographic locations to catch region-specific failures or latency regressions.

```python
import asyncio
import httpx
import json
import time
from dataclasses import dataclass

@dataclass
class RegionProbeResult:
    region: str
    ok: bool
    latency_ms: float
    error: str = ""

# Each region has a probe runner endpoint (could be Lambda, Cloud Run, or Fly.io)
REGION_RUNNERS = {
    "us-east-1": "https://probe-us-east.internal/run",
    "eu-west-1": "https://probe-eu-west.internal/run",
    "ap-southeast-1": "https://probe-ap.internal/run",
}

PROBE_PAYLOAD = {
    "prompt": "What is the capital of France?",
    "expected_substring": "Paris",
    "max_latency_ms": 5000,
}

async def probe_from_region(region: str, runner_url: str) -> RegionProbeResult:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(runner_url, json=PROBE_PAYLOAD)
            data = resp.json()
            return RegionProbeResult(
                region=region,
                ok=data.get("ok", False),
                latency_ms=data.get("latency_ms", (time.monotonic()-t0)*1000),
                error=data.get("error", ""),
            )
    except Exception as e:
        return RegionProbeResult(
            region=region,
            ok=False,
            latency_ms=(time.monotonic()-t0)*1000,
            error=str(e),
        )

async def multi_region_probe() -> list[RegionProbeResult]:
    tasks = [probe_from_region(r, u) for r, u in REGION_RUNNERS.items()]
    return await asyncio.gather(*tasks)

async def alert_on_regional_failure(results: list[RegionProbeResult]):
    failed = [r for r in results if not r.ok]
    if len(failed) == len(results):
        # All regions failing = global outage
        await send_pagerduty_alert(severity="critical", msg="Global agent outage detected by synthetic monitors")
    elif failed:
        # Partial = regional issue
        regions = [r.region for r in failed]
        await send_pagerduty_alert(severity="warning", msg=f"Regional agent degradation: {regions}")

async def send_pagerduty_alert(severity: str, msg: str):
    import logging
    logging.getLogger(__name__).error("pagerduty_alert", extra={"severity": severity, "msg": msg})

async def multi_region_loop():
    while True:
        results = await multi_region_probe()
        await alert_on_regional_failure(results)
        await asyncio.sleep(300)  # every 5 minutes
```

**When to use**: Multi-region agent deployments. Detects routing failures, CDN issues, and regional API problems.

---

## Solution 5: Semantic Correctness Probe Using LLM-as-Judge

Use a secondary LLM to judge whether probe responses are semantically correct, not just substring-matched.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SEMANTIC_PROBES = [
    {
        "name": "summarization_quality",
        "prompt": "Summarize in one sentence: The quick brown fox jumps over the lazy dog.",
        "rubric": "The summary must mention movement or jumping by an animal over another animal.",
    },
    {
        "name": "reasoning_check",
        "prompt": "If Alice is taller than Bob, and Bob is taller than Carol, who is shortest?",
        "rubric": "The answer must identify Carol as the shortest person.",
    },
]

async def judge_response(probe_name: str, response: str, rubric: str) -> tuple[bool, str]:
    """Use claude-haiku as judge for semantic correctness."""
    judge_prompt = f"""You are a test judge. Evaluate whether the following response satisfies the rubric.

Response: {response}

Rubric: {rubric}

Reply with JSON: {{"pass": true/false, "reason": "..."}}"""

    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    try:
        data = json.loads(msg.content[0].text)
        return data["pass"], data.get("reason", "")
    except Exception:
        return False, "judge_parse_error"

async def run_semantic_probe(probe: dict) -> dict:
    import time
    t0 = time.monotonic()
    try:
        # Get agent response
        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": probe["prompt"]}],
            ),
            timeout=10.0,
        )
        response_text = msg.content[0].text
        latency_ms = (time.monotonic() - t0) * 1000

        # Judge correctness
        passed, reason = await judge_response(probe["name"], response_text, probe["rubric"])
        return {
            "probe": probe["name"],
            "ok": passed,
            "latency_ms": round(latency_ms, 1),
            "reason": reason,
        }
    except asyncio.TimeoutError:
        return {"probe": probe["name"], "ok": False, "error": "timeout"}

async def semantic_monitor_loop():
    import logging
    logger = logging.getLogger(__name__)
    while True:
        results = await asyncio.gather(*[run_semantic_probe(p) for p in SEMANTIC_PROBES])
        failed = [r for r in results if not r.get("ok")]
        if failed:
            logger.error("semantic_probe_failed", extra={"failed": failed})
        else:
            logger.info("semantic_probe_passed", extra={"count": len(results)})
        await asyncio.sleep(300)
```

**When to use**: Agents where output quality matters as much as availability. Substring matching is insufficient.

---

## Solution 6: Synthetic Monitor with Alert Suppression and Escalation Policy

Avoid alert fatigue with a tiered escalation: first failure → warning, 3 consecutive failures → page on-call.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

@dataclass
class ProbeState:
    name: str
    consecutive_failures: int = 0
    last_ok_ts: float = field(default_factory=time.monotonic)
    alerted: bool = False
    suppressed_until: float = 0.0

    WARNING_THRESHOLD = 1
    CRITICAL_THRESHOLD = 3
    ALERT_COOLDOWN = 300.0  # don't re-page for 5 minutes

    def record(self, ok: bool) -> str | None:
        """Returns alert level or None."""
        if ok:
            was_failed = self.consecutive_failures >= self.WARNING_THRESHOLD
            self.consecutive_failures = 0
            self.last_ok_ts = time.monotonic()
            if was_failed and self.alerted:
                self.alerted = False
                return "resolved"
            return None

        self.consecutive_failures += 1
        now = time.monotonic()

        if now < self.suppressed_until:
            return None  # suppressed

        if self.consecutive_failures >= self.CRITICAL_THRESHOLD and not self.alerted:
            self.alerted = True
            self.suppressed_until = now + self.ALERT_COOLDOWN
            return "critical"
        elif self.consecutive_failures >= self.WARNING_THRESHOLD:
            return "warning"

        return None

class EscalatingMonitor:
    def __init__(self):
        self._states: dict[str, ProbeState] = defaultdict(lambda: ProbeState(name=""))

    async def run_probe(self, name: str, fn) -> bool:
        try:
            ok = await asyncio.wait_for(fn(), timeout=10.0)
        except Exception:
            ok = False

        state = self._states[name]
        state.name = name
        alert_level = state.record(ok)

        if alert_level == "critical":
            await self._page_oncall(name, state)
        elif alert_level == "warning":
            logger.warning("synthetic_probe_warning", extra={"probe": name, "failures": state.consecutive_failures})
        elif alert_level == "resolved":
            await self._notify_resolved(name, state)

        return ok

    async def _page_oncall(self, name: str, state: ProbeState):
        downtime = time.monotonic() - state.last_ok_ts
        logger.critical(
            "synthetic_probe_critical",
            extra={
                "probe": name,
                "consecutive_failures": state.consecutive_failures,
                "downtime_s": round(downtime, 0),
                "action": "paging_oncall",
            },
        )
        # Hook into PagerDuty / OpsGenie / Slack here

    async def _notify_resolved(self, name: str, state: ProbeState):
        logger.info("synthetic_probe_resolved", extra={"probe": name})

monitor = EscalatingMonitor()

async def escalating_loop():
    probes = {
        "math": lambda: check_math_probe(),
        "tool_flow": lambda: check_tool_flow_probe(),
        "latency": lambda: check_latency_probe(),
    }
    while True:
        await asyncio.gather(*[monitor.run_probe(n, fn) for n, fn in probes.items()])
        await asyncio.sleep(60)

# Stubs
async def check_math_probe() -> bool: return True
async def check_tool_flow_probe() -> bool: return True
async def check_latency_probe() -> bool: return True
```

**When to use**: Production agents where noisy alerts cause on-call burnout. Always suppress before escalating.

---

## Comparison

| Solution | Correctness Check | Latency Tracked | Multi-Region | Cost | Alert Policy | Best For |
|---|---|---|---|---|---|---|
| Canary with substring | Partial | Yes | No | Minimal | Basic logging | Getting started |
| Tool-flow end-to-end | Yes (full flow) | Yes | No | Low | Logging | Tool-use agents |
| Prometheus metrics | Yes | Yes (histogram) | No | Low | Grafana alerts | Existing Prom stack |
| Multi-region dispatcher | Yes | Yes (per region) | Yes | Medium | PagerDuty | Geo-distributed agents |
| LLM-as-judge semantic | Yes (semantic) | Yes | No | Medium | Logging | Quality-sensitive agents |
| Escalation policy | Yes | Partial | No | Minimal | Tiered paging | Reducing alert fatigue |

**Rule of thumb**: Run canary probes every minute, semantic probes every 5 minutes. Never alert on a single failure — require 3 consecutive to page on-call.
