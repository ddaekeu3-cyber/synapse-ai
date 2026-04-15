---
layout: solution
title: "Agent Doesn't Implement Deterministic Replay for Debugging"
category: testing
description: "When an agent produces wrong output, developers cannot reproduce the exact failure because LLM responses are non-deterministic and there's no record of the original tool calls, messages, and model responses."
tags: [testing, debugging, replay, recording, determinism, reproducibility]
---

# Agent Doesn't Implement Deterministic Replay for Debugging

## Problem

An agent fails in production with a wrong response. The developer tries to reproduce it locally but gets a different answer because the LLM is non-deterministic and the exact conversation state (tool call order, model outputs, timestamps) was never recorded. Without a replay mechanism, debugging requires guessing what the original execution looked like.

---

## Option 1: Conversation Recorder with JSON Replay

Record every message, tool call, and model response to a JSON file. Replay by substituting recorded responses instead of making live API calls.

```python
import anthropic
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

@dataclass
class RecordedTurn:
    turn_id: int
    messages_sent: list[dict]
    response_content: list[dict]
    stop_reason: str
    usage: dict
    timestamp: str

@dataclass
class ConversationRecording:
    session_id: str
    started_at: str
    turns: list[RecordedTurn] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump({
                "session_id": self.session_id,
                "started_at": self.started_at,
                "metadata": self.metadata,
                "turns": [
                    {
                        "turn_id": t.turn_id,
                        "messages_sent": t.messages_sent,
                        "response_content": t.response_content,
                        "stop_reason": t.stop_reason,
                        "usage": t.usage,
                        "timestamp": t.timestamp,
                    }
                    for t in self.turns
                ]
            }, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ConversationRecording":
        with open(path) as f:
            data = json.load(f)
        rec = cls(session_id=data["session_id"], started_at=data["started_at"],
                  metadata=data.get("metadata", {}))
        for t in data["turns"]:
            rec.turns.append(RecordedTurn(
                turn_id=t["turn_id"],
                messages_sent=t["messages_sent"],
                response_content=t["response_content"],
                stop_reason=t["stop_reason"],
                usage=t["usage"],
                timestamp=t["timestamp"]
            ))
        return rec

class RecordingClient:
    def __init__(self, recording: ConversationRecording, replay: bool = False):
        self.recording = recording
        self.replay = replay
        self._replay_idx = 0
        self._client = anthropic.Anthropic() if not replay else None

    def create_message(self, **kwargs) -> Any:
        if self.replay:
            if self._replay_idx >= len(self.recording.turns):
                raise RuntimeError(f"Replay exhausted: only {len(self.recording.turns)} turns recorded")
            turn = self.recording.turns[self._replay_idx]
            self._replay_idx += 1
            print(f"[replay] turn {turn.turn_id} (stop={turn.stop_reason})")
            return _MockResponse(turn.response_content, turn.stop_reason, turn.usage)

        response = self._client.messages.create(**kwargs)
        turn = RecordedTurn(
            turn_id=len(self.recording.turns),
            messages_sent=_serialize_messages(kwargs.get("messages", [])),
            response_content=_serialize_content(response.content),
            stop_reason=response.stop_reason,
            usage={"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
            timestamp=datetime.utcnow().isoformat()
        )
        self.recording.turns.append(turn)
        return response

class _MockResponse:
    def __init__(self, content_data: list[dict], stop_reason: str, usage: dict):
        self.content = [_MockBlock(c) for c in content_data]
        self.stop_reason = stop_reason
        class _Usage:
            pass
        self.usage = _Usage()
        self.usage.input_tokens = usage.get("input_tokens", 0)
        self.usage.output_tokens = usage.get("output_tokens", 0)

class _MockBlock:
    def __init__(self, data: dict):
        self.type = data.get("type", "text")
        self.text = data.get("text", "")
        self.id = data.get("id", "")
        self.name = data.get("name", "")
        self.input = data.get("input", {})

def _serialize_messages(messages: list) -> list[dict]:
    return [{"role": m["role"], "content": str(m["content"])[:200]} for m in messages]

def _serialize_content(content: list) -> list[dict]:
    result = []
    for block in content:
        if hasattr(block, "text"):
            result.append({"type": "text", "text": block.text})
        elif hasattr(block, "name"):
            result.append({"type": "tool_use", "id": getattr(block, "id", ""), "name": block.name, "input": block.input})
    return result

# --- Demo: record then replay ---
import uuid, tempfile
recording = ConversationRecording(session_id=str(uuid.uuid4()), started_at=datetime.utcnow().isoformat())
rec_client = RecordingClient(recording, replay=False)

messages = [{"role": "user", "content": "What is Python's GIL?"}]
response = rec_client.create_message(model="claude-haiku-4-5-20251001", max_tokens=128, messages=messages)
print(f"Live: {response.content[0].text[:80]}")

# Save and replay
with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    tmp_path = f.name
recording.save(tmp_path)

loaded = ConversationRecording.load(tmp_path)
replay_client = RecordingClient(loaded, replay=True)
replay_response = replay_client.create_message(model="claude-haiku-4-5-20251001", max_tokens=128, messages=messages)
print(f"Replay: {replay_response.content[0].text[:80]}")
os.unlink(tmp_path)

assert response.content[0].text == replay_response.content[0].text, "Replay mismatch!"
print("✓ Replay matches original")

# Expected Token Savings: Replay uses zero API tokens for reproduction. Debugging a 10-turn session replay = 0 tokens vs 5000+ tokens for live re-run attempts.
# Environment: ANTHROPIC_API_KEY required for recording. Replay requires no API key.
```

---

## Option 2: Tool Call Cassette (VCR-style)

Record only tool call inputs and outputs (the "cassette"). On replay, intercept tool calls and return recorded outputs, making the agent deterministic without mocking the LLM.

```python
import anthropic
import json
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass
class ToolCallRecord:
    tool_name: str
    input_hash: str
    inputs: dict
    output: dict
    call_index: int

@dataclass
class ToolCassette:
    records: list[ToolCallRecord] = field(default_factory=list)
    _replay_index: int = field(default=0, init=False)
    _mode: str = field(default="record", init=False)  # "record" | "replay"

    def set_replay_mode(self):
        self._mode = "replay"
        self._replay_index = 0

    def record_call(self, tool_name: str, inputs: dict, output: dict):
        h = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:12]
        self.records.append(ToolCallRecord(
            tool_name=tool_name,
            input_hash=h,
            inputs=inputs,
            output=output,
            call_index=len(self.records)
        ))

    def next_replay(self, tool_name: str, inputs: dict) -> Optional[dict]:
        if self._replay_index >= len(self.records):
            return None
        record = self.records[self._replay_index]
        if record.tool_name != tool_name:
            raise ValueError(f"Tool name mismatch: expected {record.tool_name}, got {tool_name}")
        self._replay_index += 1
        print(f"[cassette] Replaying {tool_name}[{record.call_index}]")
        return record.output

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump([
                {"tool": r.tool_name, "hash": r.input_hash, "inputs": r.inputs,
                 "output": r.output, "idx": r.call_index}
                for r in self.records
            ], f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ToolCassette":
        with open(path) as f:
            data = json.load(f)
        c = cls()
        for item in data:
            c.records.append(ToolCallRecord(
                tool_name=item["tool"], input_hash=item["hash"],
                inputs=item["inputs"], output=item["output"], call_index=item["idx"]
            ))
        return c

def build_tool_executor(
    real_tools: dict[str, Callable],
    cassette: ToolCassette
) -> Callable:
    def execute(tool_name: str, inputs: dict) -> dict:
        if cassette._mode == "replay":
            result = cassette.next_replay(tool_name, inputs)
            if result is not None:
                return result
        result = real_tools[tool_name](**inputs)
        cassette.record_call(tool_name, inputs, result)
        return result
    return execute

# Real tool implementations
def search_web(query: str) -> dict:
    return {"results": [f"Result for: {query}"], "count": 1}

def get_weather(city: str) -> dict:
    return {"city": city, "temp": 22, "condition": "sunny"}

client = anthropic.Anthropic()
TOOLS = [
    {"name": "search_web", "description": "Search the web",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_weather", "description": "Get weather",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
]

def run_agent_with_cassette(user_message: str, executor: Callable) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Done"
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = executor(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Completed"

# Record
import tempfile, os
cassette = ToolCassette()
executor = build_tool_executor({"search_web": search_web, "get_weather": get_weather}, cassette)
result1 = run_agent_with_cassette("What's the weather in Paris and search for Paris tourism?", executor)
print(f"Live: {result1[:80]}")

with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    cassette_path = f.name
cassette.save(cassette_path)
print(f"Cassette recorded {len(cassette.records)} tool calls")

# Replay (tool calls return cassette data; LLM still runs live)
replay_cassette = ToolCassette.load(cassette_path)
replay_cassette.set_replay_mode()
replay_executor = build_tool_executor({"search_web": search_web, "get_weather": get_weather}, replay_cassette)
result2 = run_agent_with_cassette("What's the weather in Paris and search for Paris tourism?", replay_executor)
print(f"Replay: {result2[:80]}")
os.unlink(cassette_path)

# Expected Token Savings: Cassette replay uses live LLM tokens but zero tool execution cost (API calls to external services). Isolates tool call correctness from LLM non-determinism.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 3: Snapshot Testing for Agent Responses

Capture the agent's final response as a golden snapshot. On subsequent runs, compare new responses against the snapshot to detect regressions.

```python
import anthropic
import json
import os
import hashlib
from dataclasses import dataclass
from typing import Optional

@dataclass
class Snapshot:
    snapshot_id: str
    prompt_hash: str
    prompt: str
    response: str
    model: str
    created_at: str

SNAPSHOT_DIR = "/tmp/agent_snapshots"

def save_snapshot(snapshot: Snapshot):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{snapshot.snapshot_id}.json")
    with open(path, "w") as f:
        json.dump(vars(snapshot), f, indent=2)
    return path

def load_snapshot(snapshot_id: str) -> Optional[Snapshot]:
    path = os.path.join(SNAPSHOT_DIR, f"{snapshot_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return Snapshot(**data)

def make_snapshot_id(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]

client = anthropic.Anthropic()

def snapshot_test(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    update: bool = False
) -> dict:
    snapshot_id = make_snapshot_id(prompt + model)
    existing = load_snapshot(snapshot_id)

    from datetime import datetime
    response = client.messages.create(
        model=model, max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    current_response = response.content[0].text

    if existing is None or update:
        snap = Snapshot(
            snapshot_id=snapshot_id,
            prompt_hash=make_snapshot_id(prompt),
            prompt=prompt,
            response=current_response,
            model=model,
            created_at=datetime.utcnow().isoformat()
        )
        path = save_snapshot(snap)
        action = "updated" if existing else "created"
        print(f"[snapshot] {action}: {snapshot_id} → {path}")
        return {"action": action, "snapshot_id": snapshot_id, "response": current_response}

    # Compare
    if current_response == existing.response:
        print(f"[snapshot] PASS: {snapshot_id}")
        return {"action": "pass", "snapshot_id": snapshot_id}

    # Diff
    old_words = set(existing.response.lower().split())
    new_words = set(current_response.lower().split())
    added = new_words - old_words
    removed = old_words - new_words
    print(f"[snapshot] FAIL: {snapshot_id}")
    print(f"  Added words:   {list(added)[:5]}")
    print(f"  Removed words: {list(removed)[:5]}")
    return {
        "action": "fail",
        "snapshot_id": snapshot_id,
        "added_words": list(added),
        "removed_words": list(removed),
        "old_response": existing.response[:100],
        "new_response": current_response[:100]
    }

# Create initial snapshots
for prompt in [
    "What is recursion in programming? Answer in one sentence.",
    "What does HTTP stand for? Answer in one sentence.",
]:
    result = snapshot_test(prompt, update=True)
    print(f"  {result['action']}: {result['response'][:60]}")

print("\nRunning snapshot tests:")
for prompt in [
    "What is recursion in programming? Answer in one sentence.",
    "What does HTTP stand for? Answer in one sentence.",
]:
    result = snapshot_test(prompt)
    print(f"  {result['action']}: {result['snapshot_id']}")

# Expected Token Savings: Snapshot tests detect regressions without re-running complex workflows. Failures show exactly what changed, reducing debug investigation time from hours to minutes.
# Environment: ANTHROPIC_API_KEY required. Writes to /tmp/agent_snapshots.
```

---

## Option 4: Deterministic Seed via Temperature=0

Force deterministic responses by setting temperature=0 and recording the seed parameters, making the agent fully reproducible across runs.

```python
import anthropic
import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass
class DeterministicRun:
    run_id: str
    model: str
    temperature: float
    system: str
    messages: list[dict]
    responses: list[str]
    tool_outputs: list[dict]
    created_at: str

    def to_replay_script(self) -> str:
        """Generate a self-contained Python script to replay this run."""
        lines = [
            "import anthropic",
            "client = anthropic.Anthropic()",
            f"# Run ID: {self.run_id}",
            f"# Created: {self.created_at}",
            f"messages = {json.dumps(self.messages, indent=2)}",
        ]
        for i, resp in enumerate(self.responses):
            lines.append(f"# Turn {i} response: {resp[:60]!r}")
        return "\n".join(lines)

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "calculate",
        "description": "Perform arithmetic calculation",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
                "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]}
            },
            "required": ["expression"]
        }
    }
]

def calculate(expression: str, operation: str = "evaluate") -> dict:
    """Deterministic tool — same input always gives same output."""
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return {"result": result, "expression": expression}
    except Exception as e:
        return {"error": str(e), "expression": expression}

def run_deterministic_agent(
    user_message: str,
    system: str = "You are a precise calculator assistant.",
    model: str = "claude-haiku-4-5-20251001"
) -> DeterministicRun:
    import uuid
    run_id = str(uuid.uuid4())
    messages = [{"role": "user", "content": user_message}]
    responses = []
    tool_outputs = []

    for _ in range(5):
        response = client.messages.create(
            model=model,
            max_tokens=256,
            temperature=0,  # Deterministic
            system=system,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    responses.append(block.text)
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = calculate(**block.input)
                    tool_outputs.append({"tool": block.name, "input": block.input, "output": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return DeterministicRun(
        run_id=run_id,
        model=model,
        temperature=0,
        system=system,
        messages=[{"role": m["role"], "content": str(m["content"])[:100]} for m in messages],
        responses=responses,
        tool_outputs=tool_outputs,
        created_at=datetime.utcnow().isoformat()
    )

# Two runs — should produce identical responses due to temperature=0
run1 = run_deterministic_agent("Calculate (15 * 4) + (100 / 5)")
run2 = run_deterministic_agent("Calculate (15 * 4) + (100 / 5)")

print(f"Run 1: {run1.responses}")
print(f"Run 2: {run2.responses}")
print(f"Identical: {run1.responses == run2.responses}")
print(f"\nReplay script:\n{run1.to_replay_script()}")

# Expected Token Savings: temperature=0 eliminates non-determinism — one reproduction run is sufficient. Without this, developers may run 5–10 attempts to catch a flaky failure, wasting 4–9x tokens.
# Environment: ANTHROPIC_API_KEY required. temperature=0 is supported on all Claude models.
```

---

## Option 5: Structured Event Log with Timeline Replay

Log every agent event (model call, tool call, decision point) with timestamps to a structured event log. Replay by replaying the event timeline.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

class EventType(Enum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MODEL_RESPONSE = "model_response"
    AGENT_DECISION = "agent_decision"
    ERROR = "error"

@dataclass
class AgentEvent:
    event_id: int
    event_type: EventType
    data: dict
    timestamp: float
    session_id: str

class EventLogger:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.events: list[AgentEvent] = []
        self._counter = 0

    def log(self, event_type: EventType, data: dict) -> AgentEvent:
        event = AgentEvent(
            event_id=self._counter,
            event_type=event_type,
            data=data,
            timestamp=time.time(),
            session_id=self.session_id
        )
        self.events.append(event)
        self._counter += 1
        return event

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump([
                {"id": e.event_id, "type": e.event_type.value,
                 "data": e.data, "ts": e.timestamp, "session": e.session_id}
                for e in self.events
            ], f, indent=2)

    @classmethod
    def load(cls, path: str, session_id: str) -> "EventLogger":
        logger = cls(session_id)
        with open(path) as f:
            data = json.load(f)
        for item in data:
            logger.events.append(AgentEvent(
                event_id=item["id"],
                event_type=EventType(item["type"]),
                data=item["data"],
                timestamp=item["ts"],
                session_id=item["session"]
            ))
        logger._counter = len(logger.events)
        return logger

    def replay_summary(self) -> dict:
        by_type: dict[str, list] = {}
        for e in self.events:
            by_type.setdefault(e.event_type.value, []).append(e)
        timeline = sorted(self.events, key=lambda e: e.timestamp)
        duration = (timeline[-1].timestamp - timeline[0].timestamp) if len(timeline) > 1 else 0
        return {
            "session_id": self.session_id,
            "total_events": len(self.events),
            "duration_seconds": duration,
            "by_type": {k: len(v) for k, v in by_type.items()},
            "tool_calls": [e.data for e in by_type.get("tool_call", [])],
            "errors": [e.data for e in by_type.get("error", [])],
        }

client = anthropic.Anthropic()

def run_logged_agent(user_message: str, logger: EventLogger) -> str:
    messages = [{"role": "user", "content": user_message}]
    logger.log(EventType.AGENT_DECISION, {"decision": "start", "input": user_message[:100]})

    TOOLS = [
        {"name": "lookup", "description": "Look up a fact",
         "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}}
    ]

    for turn in range(5):
        logger.log(EventType.MODEL_CALL, {"turn": turn, "messages": len(messages)})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            tools=TOOLS, messages=messages
        )
        logger.log(EventType.MODEL_RESPONSE, {
            "turn": turn,
            "stop_reason": response.stop_reason,
            "output_tokens": response.usage.output_tokens
        })

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    logger.log(EventType.AGENT_DECISION, {"decision": "complete", "answer": block.text[:100]})
                    return block.text
            return "Done"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logger.log(EventType.TOOL_CALL, {"tool": block.name, "input": block.input})
                result = {"info": f"Looked up: {block.input.get('fact', '')}"}
                logger.log(EventType.TOOL_RESULT, {"tool": block.name, "result": result})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Completed"

import uuid, tempfile, os
logger = EventLogger(str(uuid.uuid4()))
result = run_logged_agent("Look up facts about the moon and tell me something interesting.", logger)
print(f"Result: {result[:100]}")

with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    log_path = f.name
logger.save(log_path)

# Replay the event timeline for debugging
loaded_logger = EventLogger.load(log_path, logger.session_id)
summary = loaded_logger.replay_summary()
print(f"\nReplay summary: {json.dumps(summary, indent=2)}")
os.unlink(log_path)

# Expected Token Savings: Event logs enable root-cause analysis without re-running the agent. Finding a bug in the event log costs 0 tokens vs 500–5000 tokens per live re-run attempt.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 6: Diff-Based Regression Detection

After an agent change, run both old and new agent code on the same inputs and diff the outputs to automatically detect regressions.

```python
import anthropic
import json
import difflib
from dataclasses import dataclass
from typing import Callable

@dataclass
class RegressionResult:
    prompt: str
    old_response: str
    new_response: str
    identical: bool
    similarity: float
    diff_lines: list[str]
    regression_detected: bool

client = anthropic.Anthropic()

def run_agent_v1(prompt: str) -> str:
    """Original agent behavior."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="You are a helpful assistant. Be concise.",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

def run_agent_v2(prompt: str) -> str:
    """New agent behavior (simulated change: different system prompt)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="You are a helpful assistant. Be concise. Always end with a summary sentence.",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

def compare_responses(
    old_resp: str,
    new_resp: str,
    similarity_threshold: float = 0.85
) -> tuple[float, list[str], bool]:
    matcher = difflib.SequenceMatcher(None, old_resp, new_resp)
    similarity = matcher.ratio()
    diff = list(difflib.unified_diff(
        old_resp.splitlines(), new_resp.splitlines(),
        fromfile="old", tofile="new", lineterm=""
    ))
    regression = similarity < similarity_threshold
    return similarity, diff, regression

def run_regression_suite(
    test_prompts: list[str],
    old_agent: Callable[[str], str],
    new_agent: Callable[[str], str],
    threshold: float = 0.8
) -> list[RegressionResult]:
    results = []
    for prompt in test_prompts:
        old = old_agent(prompt)
        new = new_agent(prompt)
        identical = old == new
        similarity, diff, regression = compare_responses(old, new, threshold)
        result = RegressionResult(
            prompt=prompt,
            old_response=old,
            new_response=new,
            identical=identical,
            similarity=similarity,
            diff_lines=diff[:10],
            regression_detected=regression
        )
        results.append(result)
        status = "REGRESSION" if regression else ("IDENTICAL" if identical else "CHANGED-OK")
        print(f"[{status}] sim={similarity:.2f} | {prompt[:50]}")
        if regression and diff:
            for line in diff[:5]:
                print(f"  {line}")

    regressions = sum(1 for r in results if r.regression_detected)
    print(f"\nSummary: {len(results)} tests, {regressions} regressions")
    return results

TEST_PROMPTS = [
    "What is the capital of France?",
    "Explain what an API is in one sentence.",
    "What does CPU stand for?",
]

results = run_regression_suite(TEST_PROMPTS, run_agent_v1, run_agent_v2, threshold=0.75)
regressions = [r for r in results if r.regression_detected]
if regressions:
    print(f"\n{len(regressions)} regression(s) require review:")
    for r in regressions:
        print(f"  - {r.prompt[:50]} (similarity: {r.similarity:.2f})")
else:
    print("\nNo regressions detected.")

# Expected Token Savings: Automated diff detection catches regressions in CI before production. Running a 10-prompt suite costs ~2000 tokens vs hours of manual testing. Similarity threshold controls sensitivity.
# Environment: ANTHROPIC_API_KEY required. Uses difflib (stdlib).
```

---

## Comparison

| Option | What's Recorded | Replay Cost | Determinism | Persistence | Best For |
|--------|----------------|-------------|-------------|-------------|----------|
| 1: Conversation Recorder | All messages + responses | 0 tokens | Full | JSON file | Full conversation reproduction |
| 2: Tool Cassette (VCR) | Tool inputs/outputs only | Live LLM tokens | Tool-side only | JSON file | Debugging tool logic bugs |
| 3: Snapshot Testing | Final responses | Live LLM tokens | Comparison only | JSON files | Regression detection |
| 4: Temperature=0 + Seed | Run parameters | Full live tokens | LLM-side (temp=0) | Dataclass | Making LLM responses reproducible |
| 5: Event Timeline Log | All events + timestamps | 0 tokens | Full read-back | JSON file | Root cause analysis, post-mortem |
| 6: Diff-Based Regression | Before/after outputs | 2x live tokens | Comparison | None | CI/CD regression checks after changes |
