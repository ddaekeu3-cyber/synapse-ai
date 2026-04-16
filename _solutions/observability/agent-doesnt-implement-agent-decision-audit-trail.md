---
title: "Agent Doesn't Implement Agent Decision Audit Trail"
description: "Record every significant decision, reasoning step, and action the agent takes so you can reconstruct exactly what happened and why in any session."
category: observability
difficulty: intermediate
tags: [observability, audit, logging, tracing, decision, compliance]
---

# Agent Doesn't Implement Agent Decision Audit Trail

## Problem

When an agent makes a wrong decision or produces an unexpected output, there's no record of why. Without an audit trail, debugging requires re-running the session hoping to reproduce the issue. For production agents — especially in regulated industries — the ability to reconstruct the full decision history is essential for debugging, compliance, and user trust.

---

## Option 1: Structured Decision Log with JSONL Storage

```python
import asyncio
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.AsyncAnthropic()

@dataclass
class DecisionEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""  # "user_input", "model_call", "tool_call", "decision", "output"
    data: dict = field(default_factory=dict)
    reasoning: str = ""
    tokens_used: int = 0

class AuditTrail:
    def __init__(self, session_id: str, log_path: str = "audit_trail.jsonl"):
        self.session_id = session_id
        self.log_path = Path(log_path)
        self._events: list[DecisionEvent] = []

    def record(self, event_type: str, data: dict, reasoning: str = "", tokens: int = 0):
        event = DecisionEvent(
            session_id=self.session_id,
            event_type=event_type,
            data=data,
            reasoning=reasoning,
            tokens_used=tokens
        )
        self._events.append(event)
        # Append to JSONL file
        with self.log_path.open("a") as f:
            f.write(json.dumps({
                "event_id": event.event_id,
                "session_id": event.session_id,
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "data": event.data,
                "reasoning": event.reasoning,
                "tokens_used": event.tokens_used,
            }) + "\n")
        return event

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "total_events": len(self._events),
            "by_type": {et: sum(1 for e in self._events if e.event_type == et) for et in set(e.event_type for e in self._events)},
            "total_tokens": sum(e.tokens_used for e in self._events),
            "duration_s": (self._events[-1].timestamp - self._events[0].timestamp) if len(self._events) > 1 else 0,
        }

async def audited_chat(question: str, trail: AuditTrail, history: list[dict]) -> str:
    # Record user input
    trail.record("user_input", {"message": question, "history_length": len(history)})

    # Model call
    messages = [*history, {"role": "user", "content": question}]
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=messages
    )
    reply = resp.content[0].text
    tokens = resp.usage.input_tokens + resp.usage.output_tokens

    # Record model decision
    trail.record(
        "model_output",
        {"reply": reply[:500], "stop_reason": resp.stop_reason},
        reasoning=f"Generated in response to: {question[:100]}",
        tokens=tokens
    )
    return reply

async def main():
    session_id = str(uuid.uuid4())[:8]
    trail = AuditTrail(session_id=session_id)
    history: list[dict] = []

    questions = ["What is Python?", "How does asyncio work?", "Show me an example."]
    for q in questions:
        reply = await audited_chat(q, trail, history)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": reply})
        print(f"Q: {q}\nA: {reply[:80]}\n")

    print(f"Audit summary: {trail.summary()}")
    print(f"Audit log written to: {trail.log_path}")

asyncio.run(main())
```

---

## Option 2: Reasoning Chain Capture with Inline Annotations

```python
import asyncio
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ReasoningStep:
    step_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    step_type: str = ""
    description: str = ""
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    confidence: float | None = None
    alternatives_considered: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

@dataclass
class ReasoningChain:
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    goal: str = ""
    steps: list[ReasoningStep] = field(default_factory=list)
    final_answer: str = ""

    def add_step(self, step_type: str, description: str, inputs: dict = None, outputs: dict = None, confidence: float = None) -> ReasoningStep:
        step = ReasoningStep(
            step_type=step_type,
            description=description,
            inputs=inputs or {},
            outputs=outputs or {},
            confidence=confidence
        )
        self.steps.append(step)
        return step

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "goal": self.goal,
            "steps": [
                {
                    "step_id": s.step_id,
                    "type": s.step_type,
                    "description": s.description,
                    "confidence": s.confidence,
                    "timestamp": s.timestamp,
                }
                for s in self.steps
            ],
            "final_answer": self.final_answer[:200] if self.final_answer else ""
        }

async def reasoned_answer(question: str) -> tuple[str, ReasoningChain]:
    chain = ReasoningChain(goal=question)

    # Step 1: Classify the question
    chain.add_step("classify", f"Classifying question type: {question[:60]}", inputs={"question": question})
    classify_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system='Classify as: "factual", "analytical", "creative", or "procedural". Return only the word.',
        messages=[{"role": "user", "content": question}]
    )
    q_type = classify_resp.content[0].text.strip()
    chain.steps[-1].outputs = {"question_type": q_type}
    chain.steps[-1].confidence = 0.85

    # Step 2: Select approach based on type
    approach_map = {
        "factual": "Provide accurate, sourced information.",
        "analytical": "Break down systematically with evidence.",
        "creative": "Be imaginative and original.",
        "procedural": "Give step-by-step instructions.",
    }
    approach = approach_map.get(q_type, "Be helpful and accurate.")
    chain.add_step(
        "select_approach",
        f"Selected approach: {approach}",
        inputs={"question_type": q_type},
        outputs={"approach": approach},
        confidence=0.9
    )

    # Step 3: Generate answer
    chain.add_step("generate", "Generating final answer", inputs={"approach": approach, "question": question})
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=approach,
        messages=[{"role": "user", "content": question}]
    )
    answer = resp.content[0].text
    chain.steps[-1].outputs = {"answer_length": len(answer), "stop_reason": resp.stop_reason}
    chain.steps[-1].confidence = 0.8
    chain.final_answer = answer

    return answer, chain

async def main():
    questions = [
        "What is the difference between TCP and UDP?",
        "Write a haiku about databases.",
    ]
    for q in questions:
        answer, chain = await reasoned_answer(q)
        print(f"Q: {q}")
        print(f"Reasoning chain ({len(chain.steps)} steps):")
        for step in chain.steps:
            print(f"  [{step.step_type}] {step.description[:60]} (conf={step.confidence})")
        print(f"Answer: {answer[:100]}\n")
        print(f"Chain dict: {json.dumps(chain.to_dict(), indent=2)[:200]}...\n")

asyncio.run(main())
```

---

## Option 3: Immutable Append-Only Event Store

```python
import asyncio
import anthropic
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass(frozen=True)
class ImmutableEvent:
    """Immutable audit event with cryptographic chaining."""
    event_id: str
    session_id: str
    sequence: int
    timestamp: float
    event_type: str
    payload: str  # JSON string
    prev_hash: str
    event_hash: str = field(init=False)

    def __post_init__(self):
        # Compute hash over all fields except event_hash
        content = f"{self.event_id}:{self.session_id}:{self.sequence}:{self.timestamp}:{self.event_type}:{self.payload}:{self.prev_hash}"
        object.__setattr__(self, "event_hash", hashlib.sha256(content.encode()).hexdigest()[:16])

class ImmutableAuditStore:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._events: list[ImmutableEvent] = []
        self._sequence = 0

    def append(self, event_type: str, payload: dict) -> ImmutableEvent:
        self._sequence += 1
        prev_hash = self._events[-1].event_hash if self._events else "genesis"
        event = ImmutableEvent(
            event_id=str(uuid.uuid4())[:8],
            session_id=self.session_id,
            sequence=self._sequence,
            timestamp=time.time(),
            event_type=event_type,
            payload=json.dumps(payload),
            prev_hash=prev_hash,
        )
        self._events.append(event)
        return event

    def verify_integrity(self) -> bool:
        """Verify the cryptographic chain is intact."""
        for i, event in enumerate(self._events):
            expected_prev = self._events[i-1].event_hash if i > 0 else "genesis"
            if event.prev_hash != expected_prev:
                return False
        return True

    def export(self) -> list[dict]:
        return [
            {
                "seq": e.sequence,
                "id": e.event_id,
                "type": e.event_type,
                "ts": e.timestamp,
                "hash": e.event_hash,
                "prev": e.prev_hash,
                "payload": json.loads(e.payload),
            }
            for e in self._events
        ]

store = ImmutableAuditStore(session_id="sess-001")

async def audited_call(question: str) -> str:
    store.append("request_received", {"question": question, "length": len(question)})

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    answer = resp.content[0].text

    store.append("model_response", {
        "answer_preview": answer[:100],
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    })
    return answer

async def main():
    for q in ["What is asyncio?", "How does Python handle concurrency?"]:
        answer = await audited_call(q)
        print(f"Q: {q}\nA: {answer[:80]}\n")

    print(f"Integrity check: {store.verify_integrity()}")
    exported = store.export()
    print(f"Events: {len(exported)}")
    for e in exported:
        print(f"  [{e['seq']}] {e['type']} hash={e['hash']}")

asyncio.run(main())
```

---

## Option 4: Decision Tree Recorder for Tool-Calling Agents

```python
import asyncio
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class DecisionNode:
    node_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    decision_type: str = ""
    question: str = ""
    options_considered: list[str] = field(default_factory=list)
    chosen: str = ""
    rationale: str = ""
    timestamp: float = field(default_factory=time.time)
    children: list["DecisionNode"] = field(default_factory=list)
    outcome: Any = None

class DecisionTreeRecorder:
    def __init__(self):
        self.root: DecisionNode | None = None
        self._stack: list[DecisionNode] = []

    def start_decision(self, decision_type: str, question: str) -> DecisionNode:
        node = DecisionNode(decision_type=decision_type, question=question)
        if self._stack:
            self._stack[-1].children.append(node)
        else:
            self.root = node
        self._stack.append(node)
        return node

    def resolve_decision(self, chosen: str, rationale: str = "", outcome: Any = None):
        if not self._stack:
            return
        node = self._stack[-1]
        node.chosen = chosen
        node.rationale = rationale
        node.outcome = outcome
        self._stack.pop()

    def add_option(self, option: str):
        if self._stack:
            self._stack[-1].options_considered.append(option)

    def print_tree(self, node: DecisionNode = None, indent: int = 0):
        if node is None:
            node = self.root
        if node is None:
            return
        prefix = "  " * indent
        print(f"{prefix}[{node.decision_type}] {node.question[:60]}")
        print(f"{prefix}  → Chosen: {node.chosen} | Rationale: {node.rationale[:60]}")
        for child in node.children:
            self.print_tree(child, indent + 1)

recorder = DecisionTreeRecorder()

async def tool_calling_agent(user_request: str) -> str:
    # Decision 1: How to approach this request?
    recorder.start_decision("approach_selection", f"How to handle: {user_request}")
    recorder.add_option("Direct answer")
    recorder.add_option("Use tool")
    recorder.add_option("Ask for clarification")

    approach_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system='Choose: "direct", "tool", or "clarify". Return only the word.',
        messages=[{"role": "user", "content": user_request}]
    )
    approach = approach_resp.content[0].text.strip().lower()
    recorder.resolve_decision(approach, rationale=f"Based on request complexity and type")

    if approach == "direct":
        # Decision 2: Which model to use?
        recorder.start_decision("model_selection", "Select model for direct answer")
        recorder.add_option("claude-sonnet-4-6 (high quality)")
        recorder.add_option("claude-haiku-4-5-20251001 (fast/cheap)")
        model = "claude-sonnet-4-6" if len(user_request) > 100 else "claude-haiku-4-5-20251001"
        recorder.resolve_decision(model, rationale=f"Request length: {len(user_request)} chars")

        # Decision 3: Response length
        recorder.start_decision("response_length", "Choose max_tokens")
        max_tok = 1024 if "explain" in user_request.lower() else 512
        recorder.resolve_decision(str(max_tok), rationale="Based on question complexity markers")

        resp = await client.messages.create(
            model=model, max_tokens=max_tok,
            messages=[{"role": "user", "content": user_request}]
        )
        return resp.content[0].text
    else:
        resp = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            messages=[{"role": "user", "content": user_request}]
        )
        return resp.content[0].text

async def main():
    result = await tool_calling_agent("What is Python asyncio?")
    print(f"Answer: {result[:150]}\n")
    print("Decision Tree:")
    recorder.print_tree()

asyncio.run(main())
```

---

## Option 5: Real-Time Audit Stream with Async Callbacks

```python
import asyncio
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class AuditEvent:
    event_type: str
    data: dict
    timestamp: float = field(default_factory=time.time)

AuditCallback = Callable[[AuditEvent], Awaitable[None]]

class StreamingAuditTrail:
    def __init__(self):
        self._callbacks: list[AuditCallback] = []
        self._buffer: list[AuditEvent] = []

    def on_event(self, callback: AuditCallback):
        self._callbacks.append(callback)
        return callback

    async def emit(self, event_type: str, data: dict):
        event = AuditEvent(event_type=event_type, data=data)
        self._buffer.append(event)
        await asyncio.gather(*[cb(event) for cb in self._callbacks], return_exceptions=True)

trail = StreamingAuditTrail()

# Register multiple audit handlers
@trail.on_event
async def log_to_console(event: AuditEvent):
    print(f"[AUDIT {event.event_type}] {json.dumps(event.data)[:100]}")

@trail.on_event
async def alert_on_high_tokens(event: AuditEvent):
    if event.event_type == "model_response" and event.data.get("output_tokens", 0) > 400:
        print(f"[ALERT] High token usage: {event.data['output_tokens']} tokens")

@trail.on_event
async def track_latency(event: AuditEvent):
    if event.event_type == "model_response" and "latency_ms" in event.data:
        if event.data["latency_ms"] > 3000:
            print(f"[ALERT] Slow response: {event.data['latency_ms']:.0f}ms")

async def audited_agent_call(question: str, context: list[dict]) -> str:
    await trail.emit("request", {"question": question[:100], "context_turns": len(context)})

    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[*context, {"role": "user", "content": question}]
    )
    latency = (time.time() - t0) * 1000

    await trail.emit("model_response", {
        "answer_preview": resp.content[0].text[:100],
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "latency_ms": round(latency, 1),
        "stop_reason": resp.stop_reason,
    })
    return resp.content[0].text

async def main():
    context: list[dict] = []
    for q in ["What is Python?", "How does the GIL work?", "What about asyncio?"]:
        answer = await audited_agent_call(q, context)
        context.extend([{"role": "user", "content": q}, {"role": "assistant", "content": answer}])
        print(f"A: {answer[:60]}\n")

    print(f"Total events buffered: {len(trail._buffer)}")

asyncio.run(main())
```

---

## Option 6: Compliance-Grade Audit with Redaction and Retention

```python
import asyncio
import anthropic
import json
import time
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.AsyncAnthropic()

PII_PATTERNS = [
    (r"\b[\w.+-]+@[\w-]+\.\w+\b", "[EMAIL]"),
    (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[PHONE]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
    (r"\b4[0-9]{12}(?:[0-9]{3})?\b", "[CARD]"),
]

def redact_pii(text: str) -> str:
    for pattern, replacement in PII_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text

@dataclass
class ComplianceEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""
    data: dict = field(default_factory=dict)
    redacted: bool = False
    retention_days: int = 90
    expires_at: float = field(init=False)

    def __post_init__(self):
        self.expires_at = self.timestamp + self.retention_days * 86400

class ComplianceAuditLogger:
    def __init__(self, session_id: str, log_path: str = "compliance_audit.jsonl", redact: bool = True):
        self.session_id = session_id
        self.log_path = Path(log_path)
        self.redact = redact
        self._events: list[ComplianceEvent] = []

    def _sanitize(self, data: dict) -> tuple[dict, bool]:
        """Redact PII from all string values."""
        was_redacted = False
        sanitized = {}
        for k, v in data.items():
            if isinstance(v, str):
                clean = redact_pii(v)
                was_redacted = was_redacted or clean != v
                sanitized[k] = clean
            elif isinstance(v, dict):
                inner, r = self._sanitize(v)
                sanitized[k] = inner
                was_redacted = was_redacted or r
            else:
                sanitized[k] = v
        return sanitized, was_redacted

    def log(self, event_type: str, data: dict, retention_days: int = 90) -> ComplianceEvent:
        if self.redact:
            clean_data, was_redacted = self._sanitize(data)
        else:
            clean_data, was_redacted = data, False

        event = ComplianceEvent(
            session_id=self.session_id,
            event_type=event_type,
            data=clean_data,
            redacted=was_redacted,
            retention_days=retention_days
        )
        self._events.append(event)

        with self.log_path.open("a") as f:
            f.write(json.dumps({
                "event_id": event.event_id,
                "session_id": event.session_id,
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "data": event.data,
                "redacted": event.redacted,
                "expires_at": event.expires_at,
            }) + "\n")
        return event

    def purge_expired(self):
        now = time.time()
        before = len(self._events)
        self._events = [e for e in self._events if e.expires_at > now]
        purged = before - len(self._events)
        if purged:
            print(f"[COMPLIANCE] Purged {purged} expired events")

    def compliance_report(self) -> dict:
        return {
            "session_id": self.session_id,
            "total_events": len(self._events),
            "events_with_pii_redacted": sum(1 for e in self._events if e.redacted),
            "event_types": list(set(e.event_type for e in self._events)),
            "oldest_event_age_s": time.time() - min(e.timestamp for e in self._events) if self._events else 0,
        }

logger = ComplianceAuditLogger(session_id="compliance-sess-001", redact=True)

async def compliance_chat(question: str, history: list[dict]) -> str:
    logger.log("user_input", {"message": question, "history_turns": len(history)}, retention_days=365)
    msgs = [*history, {"role": "user", "content": question}]
    resp = await client.messages.create(model="claude-sonnet-4-6", max_tokens=512, messages=msgs)
    answer = resp.content[0].text
    logger.log("model_output", {
        "answer": answer[:300],
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
    }, retention_days=365)
    return answer

async def main():
    history: list[dict] = []
    for q in [
        "What is Python? My email is user@example.com.",  # PII should be redacted
        "How does asyncio work?",
    ]:
        answer = await compliance_chat(q, history)
        history.extend([{"role": "user", "content": q}, {"role": "assistant", "content": answer}])
        print(f"Q: {q}\nA: {answer[:80]}\n")

    report = logger.compliance_report()
    print(f"Compliance report: {report}")
    print(f"Events with PII redacted: {report['events_with_pii_redacted']}")

asyncio.run(main())
```

---

## Comparison

| Option | Storage | Tamper-Proof | PII Handling | Best For |
|--------|---------|-------------|-------------|----------|
| 1 – JSONL Structured | JSONL file | No | No | General debugging |
| 2 – Reasoning Chain | In-memory | No | No | Explainable AI / XAI |
| 3 – Immutable Event Store | In-memory + hash chain | Yes (hash chain) | No | Compliance auditing |
| 4 – Decision Tree | In-memory | No | No | Tool-calling agent debugging |
| 5 – Streaming Callbacks | In-memory + callbacks | No | No | Real-time alerting |
| 6 – Compliance Grade | JSONL + TTL | No | Yes (PII redaction) | Regulated industries |

**Recommendation:** Use Option 1 for general production agents — JSONL is queryable with standard tools and zero overhead. Add Option 5's streaming callbacks for real-time alerting on anomalies (high token usage, latency spikes). Use Option 6 in regulated environments (healthcare, finance, legal) where PII redaction and retention policies are mandatory.
