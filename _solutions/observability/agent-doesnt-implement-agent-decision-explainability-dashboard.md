---
title: "Agent Doesn't Implement Agent Decision Explainability Dashboard"
description: "AI agents make routing, tool selection, and branching decisions invisibly — operators and developers can't understand why the agent took a particular action, making debugging, auditing, and trust-building impossible without a decision trace."
problem_description: |
  When an AI agent selects a tool, routes to a sub-agent, decides to retry, or chooses between multiple paths, that decision exists only as model output with no structured explanation attached. Support teams can't explain why the agent answered a certain way. Compliance teams can't audit decision rationale. Developers can't identify whether a bad outcome was caused by wrong tool selection, bad instructions, or poor model judgment. An explainability layer captures decision points with their context, alternatives considered, and confidence signals — making agent behavior transparent and auditable.
category: observability
difficulty: intermediate
tags: [explainability, observability, audit, decision-trace, transparency]
---

## Solution 1: Structured Decision Log with Context Capture

Instrument key agent decision points to emit structured log entries capturing: what decision was made, what alternatives existed, what context was considered, and what rationale was provided.

```python
import asyncio
import json
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class DecisionEntry:
    decision_id: str
    session_id: str
    timestamp: float
    decision_type: str  # "tool_selection" | "routing" | "retry" | "branch"
    decision_made: str
    alternatives: list[str]
    context_summary: str
    rationale: str
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class DecisionLogger:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._log: list[DecisionEntry] = []

    def log(
        self,
        decision_type: str,
        decision_made: str,
        alternatives: list[str],
        context_summary: str,
        rationale: str,
        confidence: float | None = None,
        **metadata,
    ) -> DecisionEntry:
        entry = DecisionEntry(
            decision_id=str(uuid.uuid4())[:8],
            session_id=self.session_id,
            timestamp=time.time(),
            decision_type=decision_type,
            decision_made=decision_made,
            alternatives=alternatives,
            context_summary=context_summary,
            rationale=rationale,
            confidence=confidence,
            metadata=metadata,
        )
        self._log.append(entry)
        return entry

    def get_log(self) -> list[DecisionEntry]:
        return list(self._log)

    def export_json(self) -> str:
        return json.dumps([e.to_dict() for e in self._log], indent=2)

    def summary(self) -> dict:
        by_type: dict[str, int] = {}
        for entry in self._log:
            by_type[entry.decision_type] = by_type.get(entry.decision_type, 0) + 1
        return {
            "session_id": self.session_id,
            "total_decisions": len(self._log),
            "by_type": by_type,
        }


TOOL_SELECTION_SYSTEM = """You are a helpful assistant with access to tools.
When selecting a tool, explain your reasoning in this format:

<tool_choice>tool_name</tool_choice>
<rationale>Why this tool over alternatives.</rationale>
<alternatives>Comma-separated list of tools you considered but rejected.</alternatives>
<confidence>0.0-1.0</confidence>

Then execute the tool and provide the final answer."""


async def explainable_tool_selection(
    client: AsyncAnthropic,
    logger: DecisionLogger,
    user_message: str,
    available_tools: list[str],
) -> str:
    import re

    tools_list = ", ".join(available_tools)
    prompt = f"Available tools: {tools_list}\n\nUser request: {user_message}"

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=TOOL_SELECTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text

    # Extract decision components
    tool_match = re.search(r'<tool_choice>(.*?)</tool_choice>', text)
    rationale_match = re.search(r'<rationale>(.*?)</rationale>', text, re.DOTALL)
    alternatives_match = re.search(r'<alternatives>(.*?)</alternatives>', text)
    confidence_match = re.search(r'<confidence>([\d.]+)</confidence>', text)

    tool_chosen = tool_match.group(1).strip() if tool_match else "unknown"
    rationale = rationale_match.group(1).strip() if rationale_match else ""
    alts = [a.strip() for a in alternatives_match.group(1).split(",") if a.strip()] if alternatives_match else []
    confidence = float(confidence_match.group(1)) if confidence_match else None

    logger.log(
        decision_type="tool_selection",
        decision_made=tool_chosen,
        alternatives=alts,
        context_summary=user_message[:100],
        rationale=rationale,
        confidence=confidence,
        response_tokens=response.usage.output_tokens,
    )

    return text


# Usage
async def main():
    client = AsyncAnthropic()
    logger = DecisionLogger(session_id="sess_explainability_demo")

    tools = ["web_search", "calculator", "database_query", "file_reader", "code_executor"]

    questions = [
        "What is the current population of Tokyo?",
        "Calculate 15% tip on a $87.50 dinner bill.",
        "Find all users who signed up last month.",
    ]

    for q in questions:
        await explainable_tool_selection(client, logger, q, tools)

    print("Decision Log:")
    for entry in logger.get_log():
        print(f"\n[{entry.decision_type}] → {entry.decision_made} (confidence={entry.confidence})")
        print(f"  Rationale: {entry.rationale[:100]}")
        print(f"  Alternatives: {entry.alternatives}")

    print(f"\nSummary: {logger.summary()}")

asyncio.run(main())
```

## Solution 2: Real-Time Decision Tree Visualization

Build a tree structure of agent decisions during a session — capturing branches, reversals, and final paths — exportable as JSON for dashboard rendering.

```python
import asyncio
import json
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DecisionNode:
    node_id: str
    parent_id: str | None
    decision_type: str
    label: str
    chosen: bool  # Was this path taken?
    timestamp: float
    depth: int
    children: list["DecisionNode"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "decision_type": self.decision_type,
            "label": self.label,
            "chosen": self.chosen,
            "timestamp": self.timestamp,
            "depth": self.depth,
            "children": [c.to_dict() for c in self.children],
            "metadata": self.metadata,
        }


class DecisionTreeTracer:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._root = DecisionNode(
            node_id="root",
            parent_id=None,
            decision_type="session_start",
            label=f"Session {session_id}",
            chosen=True,
            timestamp=time.time(),
            depth=0,
        )
        self._current_path: list[DecisionNode] = [self._root]

    @property
    def current_node(self) -> DecisionNode:
        return self._current_path[-1]

    def branch(
        self,
        options: list[str],
        chosen_index: int,
        decision_type: str,
        metadata: dict | None = None,
    ) -> DecisionNode:
        parent = self.current_node
        depth = parent.depth + 1

        # Add all options as children, marking the chosen one
        chosen_node = None
        for i, option in enumerate(options):
            node = DecisionNode(
                node_id=str(uuid.uuid4())[:8],
                parent_id=parent.node_id,
                decision_type=decision_type,
                label=option,
                chosen=(i == chosen_index),
                timestamp=time.time(),
                depth=depth,
                metadata=metadata or {},
            )
            parent.children.append(node)
            if i == chosen_index:
                chosen_node = node

        self._current_path.append(chosen_node)
        return chosen_node

    def complete_branch(self, result: str, success: bool = True):
        current = self.current_node
        current.metadata["result"] = result[:200]
        current.metadata["success"] = success
        if len(self._current_path) > 1:
            self._current_path.pop()

    def export(self) -> dict:
        return {
            "session_id": self.session_id,
            "tree": self._root.to_dict(),
        }

    def chosen_path(self) -> list[str]:
        """Return labels of all chosen nodes in order."""
        result = []
        def traverse(node: DecisionNode):
            if node.chosen and node.node_id != "root":
                result.append(node.label)
            for child in node.children:
                if child.chosen:
                    traverse(child)
        traverse(self._root)
        return result


class TracedAgent:
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self.tracer = DecisionTreeTracer(session_id=f"sess_{int(time.time())}")

    async def _classify_intent(self, message: str) -> str:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system="Classify as: factual, analytical, creative, or technical. One word only.",
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text.strip().lower()

    async def handle(self, user_message: str) -> str:
        # Decision 1: classify intent
        intent = await self._classify_intent(user_message)
        intent_options = ["factual", "analytical", "creative", "technical"]
        idx = intent_options.index(intent) if intent in intent_options else 0
        self.tracer.branch(intent_options, idx, "intent_classification")

        # Decision 2: choose model based on intent
        model_map = {
            "factual": ("claude-haiku-4-5-20251001", 0),
            "analytical": ("claude-sonnet-4-6", 1),
            "creative": ("claude-sonnet-4-6", 1),
            "technical": ("claude-haiku-4-5-20251001", 0),
        }
        model_options = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
        chosen_model, model_idx = model_map.get(intent, ("claude-haiku-4-5-20251001", 0))
        self.tracer.branch(model_options, model_idx, "model_selection",
                           metadata={"intent": intent})

        # Execute with chosen model
        response = await self.client.messages.create(
            model=chosen_model,
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        answer = response.content[0].text
        self.tracer.complete_branch(answer[:100], success=True)

        return answer


# Usage
async def main():
    client = AsyncAnthropic()
    agent = TracedAgent(client)

    questions = [
        "What is the capital of France?",
        "Analyze the tradeoffs between REST and GraphQL.",
    ]

    for q in questions:
        answer = await agent.handle(q)
        print(f"Q: {q}")
        print(f"A: {answer[:100]}")
        print(f"Decision path: {agent.tracer.chosen_path()}\n")

    print(f"Full trace: {json.dumps(agent.tracer.export(), indent=2)[:500]}...")

asyncio.run(main())
```

## Solution 3: LLM-Powered Decision Narrator

Ask the model to explain its own decision-making process in plain English after each significant action — producing human-readable explanations that non-technical stakeholders can understand.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Any


@dataclass
class NarratedDecision:
    action: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    explanation: str
    key_factors: list[str]
    alternatives_rejected: list[str]


NARRATOR_SYSTEM = """You are an AI decision explainer.
Given an action taken by an AI agent, its inputs, and outputs, explain the decision in plain English.

Reply in this JSON format:
{
  "explanation": "1-2 sentence plain English explanation of why this action was taken",
  "key_factors": ["factor1", "factor2", "factor3"],
  "alternatives_rejected": ["alt1 and why it was rejected", "alt2 and why"]
}"""


async def narrate_decision(
    client: AsyncAnthropic,
    action: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    context: str = "",
) -> NarratedDecision:
    import json

    prompt = f"""Action taken: {action}

Inputs:
{json.dumps(inputs, indent=2)}

Outputs:
{json.dumps(outputs, indent=2)}

Context: {context}

Explain this decision."""

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=NARRATOR_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        data = json.loads(response.content[0].text)
        return NarratedDecision(
            action=action,
            inputs=inputs,
            outputs=outputs,
            explanation=data.get("explanation", ""),
            key_factors=data.get("key_factors", []),
            alternatives_rejected=data.get("alternatives_rejected", []),
        )
    except json.JSONDecodeError:
        return NarratedDecision(
            action=action, inputs=inputs, outputs=outputs,
            explanation=response.content[0].text,
            key_factors=[], alternatives_rejected=[],
        )


class NarratingAgent:
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self.decisions: list[NarratedDecision] = []

    async def complete_with_narration(self, user_message: str, system_prompt: str) -> str:
        # Execute the main request
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        answer = response.content[0].text

        # Narrate the decision
        narration = await narrate_decision(
            self.client,
            action="generate_response",
            inputs={"user_message": user_message, "system_prompt": system_prompt[:100]},
            outputs={"response_length": len(answer), "response_preview": answer[:100]},
            context=f"Responded to user with {response.usage.output_tokens} tokens",
        )
        self.decisions.append(narration)

        return answer

    def explain_session(self) -> str:
        lines = ["Session Decision Explanations:", "=" * 40]
        for i, d in enumerate(self.decisions, 1):
            lines.append(f"\nDecision {i}: {d.action}")
            lines.append(f"  {d.explanation}")
            if d.key_factors:
                lines.append(f"  Key factors: {', '.join(d.key_factors)}")
            if d.alternatives_rejected:
                lines.append(f"  Rejected: {d.alternatives_rejected[0][:60]}")
        return "\n".join(lines)


# Usage
async def main():
    client = AsyncAnthropic()
    agent = NarratingAgent(client)

    await agent.complete_with_narration(
        "Summarize the key benefits of microservices.",
        "You are a concise technical writer. Keep responses under 150 words."
    )

    print(agent.explain_session())

asyncio.run(main())
```

## Solution 4: Confidence-Annotated Response Pipeline

Attach a confidence score and uncertainty markers to every agent response — enabling dashboards to highlight low-confidence answers for human review.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class AnnotatedResponse:
    answer: str
    confidence: float  # 0.0–1.0
    uncertainty_markers: list[str]
    requires_human_review: bool
    review_reason: str | None


CONFIDENCE_ANNOTATION_SYSTEM = """You are a careful, calibrated assistant.
For every answer:
1. Provide the answer.
2. Rate your confidence 0.0–1.0 based on how sure you are.
3. List any specific claims you're uncertain about.

Format:
<answer>Your response here</answer>
<confidence>0.85</confidence>
<uncertain_claims>claim1 | claim2</uncertain_claims>"""


def parse_annotated_response(text: str) -> tuple[str, float, list[str]]:
    answer_match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    confidence_match = re.search(r'<confidence>([\d.]+)</confidence>', text)
    uncertain_match = re.search(r'<uncertain_claims>(.*?)</uncertain_claims>', text)

    answer = answer_match.group(1).strip() if answer_match else text
    confidence = float(confidence_match.group(1)) if confidence_match else 0.5
    uncertain = [c.strip() for c in uncertain_match.group(1).split("|") if c.strip()] \
        if uncertain_match else []

    return answer, confidence, uncertain


class ConfidenceAnnotatedAgent:
    def __init__(
        self,
        client: AsyncAnthropic,
        review_threshold: float = 0.6,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 512,
    ):
        self.client = client
        self.review_threshold = review_threshold
        self.model = model
        self.max_tokens = max_tokens
        self.responses: list[AnnotatedResponse] = []

    async def answer(self, question: str) -> AnnotatedResponse:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=CONFIDENCE_ANNOTATION_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        text = response.content[0].text
        answer, confidence, uncertain = parse_annotated_response(text)

        requires_review = confidence < self.review_threshold or len(uncertain) > 2
        review_reason = None
        if requires_review:
            if confidence < self.review_threshold:
                review_reason = f"Low confidence: {confidence:.2f} < {self.review_threshold}"
            elif len(uncertain) > 2:
                review_reason = f"Multiple uncertain claims: {len(uncertain)}"

        result = AnnotatedResponse(
            answer=answer,
            confidence=confidence,
            uncertainty_markers=uncertain,
            requires_human_review=requires_review,
            review_reason=review_reason,
        )
        self.responses.append(result)
        return result

    def review_queue(self) -> list[dict]:
        return [
            {
                "answer_preview": r.answer[:80],
                "confidence": r.confidence,
                "reason": r.review_reason,
                "uncertain_claims": r.uncertainty_markers,
            }
            for r in self.responses
            if r.requires_human_review
        ]


# Usage
async def main():
    client = AsyncAnthropic()
    agent = ConfidenceAnnotatedAgent(client, review_threshold=0.75)

    questions = [
        "What is the speed of light?",
        "Who was the first person to climb K2?",
        "What will the global AI market size be in 2030?",
    ]

    for q in questions:
        result = await agent.answer(q)
        status = "REVIEW" if result.requires_human_review else "OK"
        print(f"[{status}|{result.confidence:.2f}] {q}")
        print(f"  Answer: {result.answer[:80]}")
        if result.uncertainty_markers:
            print(f"  Uncertain: {result.uncertainty_markers}")

    review_q = agent.review_queue()
    print(f"\nItems needing review: {len(review_q)}")

asyncio.run(main())
```

## Solution 5: Agent Action Audit Trail with Replay Support

Record every agent action with enough context to replay it — enabling post-hoc investigation, regression testing, and compliance auditing.

```python
import asyncio
import hashlib
import json
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ActionRecord:
    action_id: str
    session_id: str
    sequence: int
    action_type: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    latency_ms: float
    timestamp: float
    fingerprint: str  # Hash of request for dedup/replay


class AuditTrail:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._records: list[ActionRecord] = []
        self._seq = 0

    def record(
        self,
        action_type: str,
        request: dict[str, Any],
        response: dict[str, Any],
        latency_ms: float,
    ) -> ActionRecord:
        self._seq += 1
        req_str = json.dumps(request, sort_keys=True)
        fingerprint = hashlib.md5(req_str.encode()).hexdigest()[:16]

        record = ActionRecord(
            action_id=str(uuid.uuid4())[:8],
            session_id=self.session_id,
            sequence=self._seq,
            action_type=action_type,
            request_payload=request,
            response_payload=response,
            latency_ms=round(latency_ms, 1),
            timestamp=time.time(),
            fingerprint=fingerprint,
        )
        self._records.append(record)
        return record

    def export(self) -> list[dict]:
        return [asdict(r) for r in self._records]

    def get_by_type(self, action_type: str) -> list[ActionRecord]:
        return [r for r in self._records if r.action_type == action_type]

    def replay_request(self, action_id: str) -> dict[str, Any] | None:
        record = next((r for r in self._records if r.action_id == action_id), None)
        return record.request_payload if record else None


class AuditedAgent:
    def __init__(self, client: AsyncAnthropic, session_id: str):
        self.client = client
        self.audit = AuditTrail(session_id)

    async def complete(
        self,
        system: str,
        user_message: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ) -> str:
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "user_message": user_message,
        }
        start = time.monotonic()
        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        text = response.content[0].text

        self.audit.record(
            action_type="model_completion",
            request=request,
            response={
                "text": text[:500],
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            latency_ms=latency_ms,
        )
        return text

    def generate_audit_report(self) -> str:
        records = self.audit.export()
        total_input = sum(r["response_payload"].get("input_tokens", 0) for r in records)
        total_output = sum(r["response_payload"].get("output_tokens", 0) for r in records)
        avg_latency = sum(r["latency_ms"] for r in records) / max(len(records), 1)

        return (
            f"Audit Report — Session {self.audit.session_id}\n"
            f"  Actions: {len(records)}\n"
            f"  Total input tokens: {total_input}\n"
            f"  Total output tokens: {total_output}\n"
            f"  Avg latency: {avg_latency:.1f}ms\n"
        )


# Usage
async def main():
    client = AsyncAnthropic()
    agent = AuditedAgent(client, session_id="audit_demo_001")

    questions = ["What is a REST API?", "Explain caching.", "What is OAuth?"]
    for q in questions:
        await agent.complete("Answer in one sentence.", q)

    print(agent.generate_audit_report())

    # Replay a specific action
    records = agent.audit.export()
    if records:
        replay = agent.audit.replay_request(records[0]["action_id"])
        print(f"Replay request for {records[0]['action_id']}: {replay}")

asyncio.run(main())
```

## Solution 6: Live Explainability Dashboard Data Feed

Emit structured explainability events to a real-time dashboard feed (SSE/WebSocket-compatible) — enabling live monitoring of agent decisions as they happen.

```python
import asyncio
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, asdict
from typing import AsyncIterator


@dataclass
class DashboardEvent:
    event_type: str  # "decision" | "completion" | "error" | "metric"
    session_id: str
    timestamp: float
    data: dict

    def to_sse(self) -> str:
        return f"event: {self.event_type}\ndata: {json.dumps(asdict(self))}\n\n"


class ExplainabilityFeed:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._queue: asyncio.Queue[DashboardEvent] = asyncio.Queue()

    def emit(self, event_type: str, data: dict):
        event = DashboardEvent(
            event_type=event_type,
            session_id=self.session_id,
            timestamp=time.time(),
            data=data,
        )
        self._queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[DashboardEvent]:
        while True:
            event = await self._queue.get()
            yield event
            if event.event_type == "session_end":
                return


class LiveExplainabilityAgent:
    def __init__(self, client: AsyncAnthropic, session_id: str):
        self.client = client
        self.feed = ExplainabilityFeed(session_id)
        self._step = 0

    async def handle(self, user_message: str) -> str:
        self._step += 1

        # Emit: processing started
        self.feed.emit("decision", {
            "step": self._step,
            "type": "request_received",
            "summary": f"Processing: {user_message[:60]}",
        })

        # Classify intent
        intent_response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system="Reply with one word: factual, analytical, or creative.",
            messages=[{"role": "user", "content": user_message}],
        )
        intent = intent_response.content[0].text.strip()

        self.feed.emit("decision", {
            "step": self._step,
            "type": "intent_classified",
            "intent": intent,
            "rationale": f"Message classified as '{intent}' based on content analysis",
        })

        # Select model based on intent
        model = "claude-sonnet-4-6" if intent == "analytical" else "claude-haiku-4-5-20251001"
        self.feed.emit("decision", {
            "step": self._step,
            "type": "model_selected",
            "model": model,
            "reason": f"Intent='{intent}' → selected {'higher-capability' if 'sonnet' in model else 'faster'} model",
        })

        # Generate response
        start = time.monotonic()
        response = await self.client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        text = response.content[0].text

        self.feed.emit("completion", {
            "step": self._step,
            "model": model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": round(latency_ms, 1),
            "response_preview": text[:100],
        })

        return text

    async def end_session(self):
        self.feed.emit("session_end", {"total_steps": self._step})


# Usage
async def main():
    client = AsyncAnthropic()
    agent = LiveExplainabilityAgent(client, session_id="live_demo_001")

    # Consumer: print SSE events as they arrive
    async def dashboard_consumer():
        async for event in agent.feed.stream():
            print(f"[{event.event_type}] {json.dumps(event.data)}")

    consumer_task = asyncio.create_task(dashboard_consumer())

    questions = ["What is a load balancer?", "Analyze the tradeoffs of microservices vs monolith."]
    for q in questions:
        await agent.handle(q)

    await agent.end_session()
    await consumer_task

asyncio.run(main())
```

## Comparison

| Approach | Audience | Granularity | Searchable | Replayable | Overhead | Best For |
|---|---|---|---|---|---|---|
| Structured Decision Log | Developers | Per-decision | Yes | Partial | Low | Engineering debugging |
| Decision Tree Visualizer | All | Full tree | Yes | No | Low | Visual audit trails |
| LLM-Powered Narrator | Business stakeholders | Per-action | No | No | Medium | Non-technical explainability |
| Confidence Annotation | QA/Compliance | Per-response | Yes | No | Low | Flagging uncertain outputs |
| Audit Trail + Replay | Compliance/Security | Per-action | Yes | Yes | Low | Regulatory auditing |
| Live Dashboard Feed | Operations | Real-time | No | No | Minimal | Live monitoring |
