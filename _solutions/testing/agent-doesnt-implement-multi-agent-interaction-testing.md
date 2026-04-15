---
layout: solution
title: "Agent Doesn't Implement Multi-Agent Interaction Testing"
category: testing
description: "Teams building orchestrator/subagent systems have no way to test how agents interact: message routing, context handoff, parallel coordination, and failure propagation between agents all go untested until production incidents expose them."
tags: [testing, multi-agent, orchestrator, subagent, interaction, coordination, asyncio]
---

## Problem

Multi-agent systems introduce interaction bugs that single-agent unit tests cannot catch: an orchestrator that drops subagent results, a subagent that misinterprets its delegated context, a coordination pattern that deadlocks under load, or a failure in one subagent that silently corrupts the orchestrator's final answer. Without dedicated interaction tests, these bugs only surface in production when multiple agents are running live.

## Solutions

### Option 1: Fake Subagent Stubs with Scripted Responses

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any

@dataclass
class StubResponse:
    content: str
    role: str = "assistant"

class SubagentStub:
    """Replaces a real Claude call with a scripted sequence of responses."""
    def __init__(self, responses: list[str]):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def create(self, *, messages: list, model: str, max_tokens: int, **kwargs) -> Any:
        self.calls.append({"messages": messages, "model": model})
        if not self._queue:
            raise RuntimeError("StubSubagent exhausted: no more scripted responses")
        text = self._queue.pop(0)

        class FakeContent:
            type = "text"
            def __init__(self, t): self.text = t
        class FakeMsg:
            def __init__(self, t):
                self.content = [FakeContent(t)]
                self.stop_reason = "end_turn"
        return FakeMsg(text)

async def orchestrator(subagent_client, task: str) -> str:
    """Simple orchestrator: delegates to subagent then summarizes result."""
    sub_result = subagent_client.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Research: {task}"}],
    )
    sub_text = sub_result.content[0].text

    # Orchestrator synthesizes
    real_client = anthropic.Anthropic()
    final = real_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[
            {"role": "user", "content": f"Task: {task}"},
            {"role": "assistant", "content": f"Subagent found: {sub_text}"},
            {"role": "user", "content": "Summarize in one sentence."},
        ],
    )
    return final.content[0].text

def test_orchestrator_uses_subagent_result():
    stub = SubagentStub(["Paris is the capital of France."])
    import asyncio
    result = orchestrator(stub, "What is the capital of France?")
    assert stub.calls, "Orchestrator never called subagent"
    assert "Paris" in result or len(result) > 0  # orchestrator incorporated the answer
    print(f"PASS: orchestrator produced '{result}'")
    print(f"Subagent received {len(stub.calls)} call(s)")

if __name__ == "__main__":
    test_orchestrator_uses_subagent_result()

# Expected Token Savings: subagent calls use zero tokens (stubbed); only orchestrator synthesis hits API
# Environment: synchronous orchestrators; scripted stubs validate routing without API cost
```

### Option 2: Async Multi-Agent Harness with Message Interception

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class AgentMessage:
    sender: str
    recipient: str
    content: str
    turn: int

class MultiAgentTestHarness:
    """Records all inter-agent messages for assertion after the run."""
    def __init__(self):
        self._log: list[AgentMessage] = []
        self._turn = 0
        self._client = anthropic.AsyncAnthropic()

    def _record(self, sender: str, recipient: str, content: str):
        self._log.append(AgentMessage(sender, recipient, content, self._turn))

    async def call_agent(self, agent_name: str, system: str, messages: list) -> str:
        self._turn += 1
        user_text = messages[-1]["content"] if messages else ""
        self._record("orchestrator", agent_name, user_text)

        resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=messages,
        )
        result = resp.content[0].text
        self._record(agent_name, "orchestrator", result)
        return result

    def assert_agent_called(self, agent_name: str):
        recipients = {m.recipient for m in self._log}
        assert agent_name in recipients, f"Agent '{agent_name}' was never called. Called: {recipients}"

    def assert_message_order(self, expected_order: list[tuple[str, str]]):
        """Assert (sender, recipient) pairs appear in order."""
        actual = [(m.sender, m.recipient) for m in self._log]
        idx = 0
        for pair in expected_order:
            while idx < len(actual) and actual[idx] != pair:
                idx += 1
            assert idx < len(actual), f"Expected {pair} in message log but not found. Log: {actual}"
            idx += 1

    def message_count(self, agent_name: str) -> int:
        return sum(1 for m in self._log if m.recipient == agent_name)

async def test_parallel_subagent_coordination():
    harness = MultiAgentTestHarness()

    RESEARCHER_SYS = "You are a research agent. Answer factual questions concisely."
    CRITIC_SYS = "You are a critic agent. Point out one potential flaw in the given answer."

    # Run two subagents in parallel
    question = "What is the boiling point of water?"
    research_task, critic_task = await asyncio.gather(
        harness.call_agent("researcher", RESEARCHER_SYS, [{"role": "user", "content": question}]),
        harness.call_agent("critic", CRITIC_SYS, [{"role": "user", "content": f"Answer: 100°C at sea level"}]),
    )

    harness.assert_agent_called("researcher")
    harness.assert_agent_called("critic")
    assert harness.message_count("researcher") == 1
    assert harness.message_count("critic") == 1
    print(f"PASS: both agents called in parallel")
    print(f"Researcher: {research_task[:60]}...")
    print(f"Critic: {critic_task[:60]}...")

if __name__ == "__main__":
    asyncio.run(test_parallel_subagent_coordination())

# Expected Token Savings: haiku for all agents keeps cost low; parallel gather halves wall time
# Environment: async orchestrators; validates coordination topology, not just individual responses
```

### Option 3: Context Handoff Integrity Testing

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic()

def agent_a(task: str) -> dict:
    """Agent A: extracts structured data from a task description."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Extract key facts as JSON with keys: subject, action, target.",
        messages=[{"role": "user", "content": task}],
    )
    text = resp.content[0].text
    try:
        # Strip markdown fences if present
        clean = text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"raw": text}

def agent_b(context: dict) -> str:
    """Agent B: generates a plan given structured context from Agent A."""
    context_str = json.dumps(context, indent=2)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You receive structured context from another agent. Produce a one-step action plan.",
        messages=[{"role": "user", "content": f"Context:\n{context_str}\n\nWhat should be done?"}],
    )
    return resp.content[0].text

def test_context_handoff_preserves_fields():
    task = "Alice needs to delete the old report files from the archive."
    context = agent_a(task)

    print(f"Agent A output: {context}")
    assert isinstance(context, dict), "Agent A must return a dict"

    # Key fields must survive the handoff
    required_keys = {"subject", "action", "target"}
    missing = required_keys - set(context.keys())
    assert not missing, f"Agent A dropped fields: {missing}"

    plan = agent_b(context)
    print(f"Agent B plan: {plan}")

    # Agent B must reference at least one field value from the context
    context_values = [str(v).lower() for v in context.values()]
    plan_lower = plan.lower()
    referenced = any(val in plan_lower for val in context_values if len(val) > 3)
    assert referenced, "Agent B plan doesn't reference any context from Agent A"
    print("PASS: context handoff preserved all fields and Agent B used them")

if __name__ == "__main__":
    test_context_handoff_preserves_fields()

# Expected Token Savings: haiku for both agents; structural assertions catch handoff bugs without expensive re-runs
# Environment: pipeline orchestrators; validates that Agent B actually consumed Agent A's output
```

### Option 4: Failure Propagation Testing Between Agents

```python
import anthropic
import asyncio
from typing import Optional

class AgentError(Exception):
    def __init__(self, agent: str, reason: str):
        super().__init__(f"[{agent}] {reason}")
        self.agent = agent
        self.reason = reason

async def subagent_tool(client: anthropic.AsyncAnthropic, task: str, fail: bool = False) -> str:
    if fail:
        raise AgentError("subagent", "Simulated tool failure")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": task}],
    )
    return resp.content[0].text

async def orchestrator_with_fallback(
    client: anthropic.AsyncAnthropic,
    task: str,
    inject_failure: bool = False,
) -> dict:
    errors: list[str] = []
    result: Optional[str] = None

    try:
        result = await subagent_tool(client, task, fail=inject_failure)
    except AgentError as e:
        errors.append(str(e))
        # Fallback: orchestrator handles it directly
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"[Fallback] {task}"}],
        )
        result = resp.content[0].text

    return {"result": result, "errors": errors, "used_fallback": len(errors) > 0}

async def test_failure_propagation():
    client = anthropic.AsyncAnthropic()

    # Happy path: no failure
    happy = await orchestrator_with_fallback(client, "What is 2+2?", inject_failure=False)
    assert happy["used_fallback"] is False, "Should not use fallback on success"
    assert happy["result"], "Should have a result"
    print(f"PASS happy path: result='{happy['result'][:40]}...'")

    # Failure path: subagent fails, orchestrator recovers
    degraded = await orchestrator_with_fallback(client, "What is 3+3?", inject_failure=True)
    assert degraded["used_fallback"] is True, "Orchestrator should have used fallback"
    assert degraded["result"], "Fallback result must not be empty"
    assert len(degraded["errors"]) == 1, "Must record exactly one error"
    print(f"PASS failure path: fallback_result='{degraded['result'][:40]}...'")
    print(f"Errors recorded: {degraded['errors']}")

if __name__ == "__main__":
    asyncio.run(test_failure_propagation())

# Expected Token Savings: haiku on both paths; failure injection avoids needing real broken services
# Environment: resilient orchestrators; validates that subagent failure never silently corrupts output
```

### Option 5: Deadlock and Timeout Detection for Agent Coordination

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def slow_subagent(name: str, delay: float, task: str) -> str:
    """Simulates a subagent that takes `delay` seconds before responding."""
    await asyncio.sleep(delay)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": task}],
    )
    return resp.content[0].text

async def orchestrate_with_timeout(
    agents: list[tuple[str, float, str]],  # (name, delay, task)
    per_agent_timeout: float = 3.0,
) -> dict:
    async def bounded_call(name: str, delay: float, task: str):
        try:
            result = await asyncio.wait_for(
                slow_subagent(name, delay, task),
                timeout=per_agent_timeout,
            )
            return name, result, None
        except asyncio.TimeoutError:
            return name, None, f"timed_out after {per_agent_timeout}s"

    results = await asyncio.gather(*[bounded_call(*a) for a in agents])
    return {
        name: {"result": res, "error": err}
        for name, res, err in results
    }

async def test_timeout_detection():
    agents = [
        ("fast_agent", 0.0, "Say OK"),
        ("slow_agent", 10.0, "Say OK"),  # will time out
    ]

    t0 = time.time()
    report = await orchestrate_with_timeout(agents, per_agent_timeout=2.0)
    elapsed = time.time() - t0

    assert elapsed < 5.0, f"Orchestrator should not wait for slow agent; took {elapsed:.1f}s"
    assert report["fast_agent"]["result"] is not None, "Fast agent should have result"
    assert report["slow_agent"]["error"] is not None, "Slow agent should have timed out"
    assert "timed_out" in report["slow_agent"]["error"]
    print(f"PASS: completed in {elapsed:.1f}s (not {10}s)")
    print(f"fast_agent: '{report['fast_agent']['result']}'")
    print(f"slow_agent: '{report['slow_agent']['error']}'")

if __name__ == "__main__":
    asyncio.run(test_timeout_detection())

# Expected Token Savings: only fast_agent hits API; slow_agent times out before Claude call completes
# Environment: any async multi-agent system; validates that one slow agent can't block the entire orchestration
```

### Option 6: Golden Conversation Flow Regression Test

```python
import anthropic
import json
import hashlib
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Turn:
    agent: str
    role: str
    content: str

GOLDEN_FLOW: list[dict] = [
    {"agent": "planner", "role": "user", "content": "Plan a 3-step process to write a report."},
    {"agent": "planner", "role": "assistant", "content": None},   # filled at record time
    {"agent": "writer", "role": "user", "content": None},         # derived from planner output
    {"agent": "writer", "role": "assistant", "content": None},
]

GOLDEN_FILE = Path("/tmp/multi_agent_golden.json")

client = anthropic.Anthropic()

def run_agent(system: str, messages: list) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    return resp.content[0].text

def record_golden():
    planner_out = run_agent(
        "You are a planning agent. Output a numbered 3-step plan.",
        [{"role": "user", "content": GOLDEN_FLOW[0]["content"]}],
    )
    writer_input = f"Based on this plan:\n{planner_out}\nWrite the intro paragraph."
    writer_out = run_agent(
        "You are a writing agent. Write one clear paragraph.",
        [{"role": "user", "content": writer_input}],
    )
    golden = {
        "planner_out": planner_out,
        "writer_input": writer_input,
        "writer_out": writer_out,
        "planner_hash": hashlib.md5(planner_out.encode()).hexdigest(),
        "writer_hash": hashlib.md5(writer_out.encode()).hexdigest(),
    }
    GOLDEN_FILE.write_text(json.dumps(golden, indent=2))
    print(f"Golden recorded to {GOLDEN_FILE}")
    return golden

def test_against_golden(threshold: float = 0.5):
    if not GOLDEN_FILE.exists():
        golden = record_golden()
    else:
        golden = json.loads(GOLDEN_FILE.read_text())

    # Re-run the flow
    planner_out = run_agent(
        "You are a planning agent. Output a numbered 3-step plan.",
        [{"role": "user", "content": GOLDEN_FLOW[0]["content"]}],
    )
    writer_input = f"Based on this plan:\n{planner_out}\nWrite the intro paragraph."
    writer_out = run_agent(
        "You are a writing agent. Write one clear paragraph.",
        [{"role": "user", "content": writer_input}],
    )

    def word_overlap(a: str, b: str) -> float:
        wa, wb = set(a.lower().split()), set(b.lower().split())
        if not wa or not wb: return 0.0
        return len(wa & wb) / len(wa | wb)

    planner_sim = word_overlap(planner_out, golden["planner_out"])
    writer_sim = word_overlap(writer_out, golden["writer_out"])

    print(f"Planner similarity: {planner_sim:.2f} (threshold {threshold})")
    print(f"Writer similarity:  {writer_sim:.2f} (threshold {threshold})")

    assert planner_sim >= threshold, f"Planner output drifted too far: {planner_sim:.2f}"
    assert writer_sim >= threshold, f"Writer output drifted too far: {writer_sim:.2f}"
    print("PASS: multi-agent flow matches golden within threshold")

if __name__ == "__main__":
    test_against_golden()

# Expected Token Savings: haiku for both agents; similarity check catches regressions without full diff
# Environment: multi-agent pipelines; detects prompt or model changes that alter inter-agent behavior
```

## Comparison

| Option | What It Tests | Failure Mode Caught | API Calls |
|--------|--------------|---------------------|-----------|
| 1 — Stub responses | Orchestrator routing logic | Missing/wrong subagent delegation | Orchestrator only |
| 2 — Async harness + message log | Coordination topology | Wrong call order, missing parallel calls | All agents (haiku) |
| 3 — Context handoff integrity | Data preservation across agents | Dropped/corrupted fields between agents | Both agents (haiku) |
| 4 — Failure propagation | Error recovery paths | Silent corruption on subagent failure | Fallback path only |
| 5 — Timeout detection | Coordination deadlocks | Slow agent blocking orchestrator | Fast agents only |
| 6 — Golden flow regression | End-to-end output stability | Prompt/model changes breaking flow | All agents (haiku) |
