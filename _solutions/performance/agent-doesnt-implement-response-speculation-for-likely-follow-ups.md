---
title: "Agent Doesn't Implement Response Speculation for Likely Follow-Ups"
description: "Pre-generate responses to predicted follow-up questions while the user reads the current answer, eliminating perceived latency."
category: performance
difficulty: advanced
tags: [speculation, prefetch, latency, caching, prediction, asyncio]
---

# Agent Doesn't Implement Response Speculation for Likely Follow-Ups

## Problem

Every user turn incurs a full round-trip to the model. When follow-up questions are predictable from context, that latency is wasted — the agent could have started generating answers while the user was still reading the previous response.

---

## Option 1: Next-Question Prediction with Haiku Prefetch

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SpeculativeCache:
    predictions: dict[str, str] = field(default_factory=dict)
    pending: dict[str, asyncio.Task] = field(default_factory=dict)

cache = SpeculativeCache()

async def predict_follow_ups(conversation: list[dict], answer: str) -> list[str]:
    """Use Haiku to predict the 3 most likely follow-up questions."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Return exactly 3 likely follow-up questions as a JSON array of strings. No other text.",
        messages=[
            *conversation,
            {"role": "assistant", "content": answer},
            {"role": "user", "content": "What are the 3 most likely follow-up questions?"}
        ]
    )
    import json
    try:
        return json.loads(resp.content[0].text)[:3]
    except Exception:
        return []

async def speculate(question: str, conversation: list[dict]) -> None:
    """Pre-generate answer for a predicted question."""
    if question in cache.predictions or question in cache.pending:
        return
    async def _gen():
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[*conversation, {"role": "user", "content": question}]
        )
        cache.predictions[question] = resp.content[0].text
        cache.pending.pop(question, None)
    task = asyncio.create_task(_gen())
    cache.pending[question] = task

async def answer(user_message: str, conversation: list[dict]) -> str:
    # Check speculation cache first
    if user_message in cache.predictions:
        result = cache.predictions.pop(user_message)
        print("[cache hit]")
        # Cancel pending tasks for stale predictions
        for t in cache.pending.values():
            t.cancel()
        cache.pending.clear()
        return result

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[*conversation, {"role": "user", "content": user_message}]
    )
    text = resp.content[0].text

    # Kick off speculation in background
    questions = await predict_follow_ups(conversation, text)
    for q in questions:
        await speculate(q, [*conversation, {"role": "user", "content": user_message}, {"role": "assistant", "content": text}])

    return text

async def main():
    conv: list[dict] = []
    for user_input in ["What is asyncio?", "How do I create a task?", "What about task cancellation?"]:
        await asyncio.sleep(0.1)  # simulate reading time
        reply = await answer(user_input, conv)
        conv.append({"role": "user", "content": user_input})
        conv.append({"role": "assistant", "content": reply})
        print(f"Q: {user_input}\nA: {reply[:80]}...\n")

asyncio.run(main())
```

---

## Option 2: Semantic Similarity Prefetch Queue

```python
import asyncio
import anthropic
import hashlib
from collections import OrderedDict

client = anthropic.AsyncAnthropic()

class SemanticPrefetchQueue:
    def __init__(self, capacity: int = 20):
        self.cache: OrderedDict[str, asyncio.Future] = OrderedDict()
        self.capacity = capacity
        self._lock = asyncio.Lock()

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.lower().strip().encode()).hexdigest()[:16]

    async def prefetch(self, questions: list[str], context: list[dict]) -> None:
        async with self._lock:
            # Evict oldest if over capacity
            while len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)

        async def _fetch(q: str):
            r = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[*context, {"role": "user", "content": q}]
            )
            return r.content[0].text

        for q in questions:
            k = self._key(q)
            async with self._lock:
                if k not in self.cache:
                    fut: asyncio.Future = asyncio.get_event_loop().create_future()
                    self.cache[k] = fut
                    asyncio.create_task(self._resolve(fut, _fetch(q)))

    async def _resolve(self, fut: asyncio.Future, coro):
        try:
            result = await coro
            if not fut.done():
                fut.set_result(result)
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)

    async def get(self, question: str, timeout: float = 0.05) -> str | None:
        k = self._key(question)
        async with self._lock:
            fut = self.cache.get(k)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            return None

queue = SemanticPrefetchQueue()

async def chat(user_msg: str, conversation: list[dict]) -> str:
    # Try prefetch cache
    cached = await queue.get(user_msg)
    if cached:
        print("[prefetch hit]")
        return cached

    # Full generation
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[*conversation, {"role": "user", "content": user_msg}]
    )
    text = resp.content[0].text

    # Predict and prefetch follow-ups
    pred_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system='Return 3 follow-up questions as JSON array ["q1","q2","q3"].',
        messages=[*conversation, {"role": "user", "content": user_msg}, {"role": "assistant", "content": text[:500]},
                  {"role": "user", "content": "Follow-ups?"}]
    )
    import json
    try:
        preds = json.loads(pred_resp.content[0].text)
        new_ctx = [*conversation, {"role": "user", "content": user_msg}, {"role": "assistant", "content": text}]
        await queue.prefetch(preds, new_ctx)
    except Exception:
        pass

    return text
```

---

## Option 3: Template-Based Speculation for Structured Domains

```python
import asyncio
import anthropic
import re
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class Template:
    pattern: str          # regex to match user message
    follow_ups: list[str] # template follow-up questions

TEMPLATES = [
    Template(r"what is (.+)\?", ["How do I use {0}?", "What are the limitations of {0}?", "Show me an example of {0}."]),
    Template(r"how (do|does|can) (.+)\?", ["Why does {1} work that way?", "What are alternatives to {1}?", "What errors can occur with {1}?"]),
    Template(r"explain (.+)", ["Can you give an example of {0}?", "How does {0} compare to alternatives?", "When should I use {0}?"]),
]

class TemplateSpeculator:
    def __init__(self):
        self._cache: dict[str, asyncio.Task] = {}

    def predict(self, user_msg: str) -> list[str]:
        for tmpl in TEMPLATES:
            m = re.search(tmpl.pattern, user_msg, re.IGNORECASE)
            if m:
                groups = m.groups()
                return [q.format(*groups) for q in tmpl.follow_ups]
        return []

    async def speculate_all(self, predictions: list[str], context: list[dict]):
        for q in predictions:
            if q not in self._cache:
                self._cache[q] = asyncio.create_task(self._generate(q, context))

    async def _generate(self, q: str, context: list[dict]) -> str:
        r = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[*context, {"role": "user", "content": q}]
        )
        return r.content[0].text

    async def retrieve(self, user_msg: str) -> str | None:
        task = self._cache.pop(user_msg, None)
        if task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=0.01)
        except asyncio.TimeoutError:
            # Task still running — wait a bit more then fall through
            self._cache[user_msg] = task
            return None

speculator = TemplateSpeculator()

async def respond(user_msg: str, conv: list[dict]) -> str:
    # Try to get speculated answer
    cached = await speculator.retrieve(user_msg)
    if cached:
        print("[template speculation hit]")
        return cached

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[*conv, {"role": "user", "content": user_msg}]
    )
    text = resp.content[0].text

    # Speculate next turn
    new_ctx = [*conv, {"role": "user", "content": user_msg}, {"role": "assistant", "content": text}]
    preds = speculator.predict(user_msg)
    await speculator.speculate_all(preds, new_ctx)

    return text
```

---

## Option 4: Streaming Response + Background Speculation Overlap

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class OverlapEngine:
    speculated: dict[str, asyncio.Task] = field(default_factory=dict)

    async def stream_and_speculate(
        self,
        user_msg: str,
        conversation: list[dict],
    ) -> str:
        """Stream the answer while simultaneously speculating next responses."""
        full_text_parts: list[str] = []
        speculation_started = False

        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[*conversation, {"role": "user", "content": user_msg}],
            system="Be concise."
        ) as stream:
            async for text in stream.text_stream:
                full_text_parts.append(text)
                print(text, end="", flush=True)

                # Start speculation once we have enough context (after ~200 chars)
                if not speculation_started and len("".join(full_text_parts)) > 200:
                    speculation_started = True
                    partial = "".join(full_text_parts)
                    asyncio.create_task(self._speculate_from_partial(
                        user_msg, partial, conversation
                    ))

        print()  # newline after stream
        return "".join(full_text_parts)

    async def _speculate_from_partial(
        self, user_msg: str, partial_answer: str, context: list[dict]
    ):
        # Predict follow-ups from partial response
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system='JSON array of 2 likely follow-up questions.',
            messages=[
                *context,
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": partial_answer + "..."},
                {"role": "user", "content": "Follow-ups?"}
            ]
        )
        import json
        try:
            qs = json.loads(r.content[0].text)
        except Exception:
            return

        new_ctx = [*context, {"role": "user", "content": user_msg}, {"role": "assistant", "content": partial_answer}]
        for q in qs[:2]:
            if q not in self.speculated:
                self.speculated[q] = asyncio.create_task(
                    self._generate(q, new_ctx)
                )

    async def _generate(self, q: str, ctx: list[dict]) -> str:
        r = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024,
            messages=[*ctx, {"role": "user", "content": q}]
        )
        return r.content[0].text

    async def get(self, q: str) -> str | None:
        task = self.speculated.pop(q, None)
        if task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        except asyncio.TimeoutError:
            self.speculated[q] = task
            return None

engine = OverlapEngine()

async def main():
    conv: list[dict] = []
    questions = [
        "Explain Python's GIL.",
        "How does threading work despite the GIL?",
        "What is multiprocessing then?"
    ]
    for q in questions:
        await asyncio.sleep(2)  # simulate reading time
        cached = await engine.get(q)
        if cached:
            print(f"\n[speculated] Q: {q}\nA: {cached[:80]}...\n")
        else:
            text = await engine.stream_and_speculate(q, conv)
            conv.append({"role": "user", "content": q})
            conv.append({"role": "assistant", "content": text})

asyncio.run(main())
```

---

## Option 5: Branching Speculation Tree

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
import json

client = anthropic.AsyncAnthropic()

@dataclass
class SpecNode:
    question: str
    answer_task: asyncio.Task | None = None
    children: list["SpecNode"] = field(default_factory=list)
    depth: int = 0

MAX_DEPTH = 2
BRANCH_FACTOR = 2

class SpeculationTree:
    def __init__(self):
        self._nodes: dict[str, SpecNode] = {}

    async def build(self, root_q: str, context: list[dict], depth: int = 0):
        if depth >= MAX_DEPTH:
            return
        node = SpecNode(question=root_q, depth=depth)
        self._nodes[root_q] = node
        node.answer_task = asyncio.create_task(self._answer(root_q, context))

        # After generating answer, build children
        try:
            answer = await asyncio.wait_for(asyncio.shield(node.answer_task), timeout=10)
        except asyncio.TimeoutError:
            return

        children_qs = await self._predict_children(root_q, answer, context)
        new_ctx = [*context, {"role": "user", "content": root_q}, {"role": "assistant", "content": answer}]

        child_tasks = [
            asyncio.create_task(self.build(cq, new_ctx, depth + 1))
            for cq in children_qs[:BRANCH_FACTOR]
        ]
        await asyncio.gather(*child_tasks, return_exceptions=True)

    async def _answer(self, q: str, ctx: list[dict]) -> str:
        r = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            messages=[*ctx, {"role": "user", "content": q}]
        )
        return r.content[0].text

    async def _predict_children(self, q: str, answer: str, ctx: list[dict]) -> list[str]:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=f'Return {BRANCH_FACTOR} follow-up questions as JSON array.',
            messages=[*ctx, {"role": "user", "content": q}, {"role": "assistant", "content": answer[:300]},
                      {"role": "user", "content": "Follow-ups?"}]
        )
        try:
            return json.loads(r.content[0].text)
        except Exception:
            return []

    async def get(self, q: str, timeout: float = 0.1) -> str | None:
        node = self._nodes.get(q)
        if node is None or node.answer_task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(node.answer_task), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            return None

    def stats(self) -> dict:
        return {"nodes": len(self._nodes), "ready": sum(1 for n in self._nodes.values() if n.answer_task and n.answer_task.done())}
```

---

## Option 6: Confidence-Gated Speculation with Cost Control

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SpeculationBudget:
    max_concurrent: int = 3
    max_input_tokens_per_spec: int = 4096
    hit_rate_window: list[bool] = field(default_factory=list)
    total_specs: int = 0
    hits: int = 0

    def hit_rate(self) -> float:
        if not self.hit_rate_window:
            return 0.5  # assume 50% initially
        return sum(self.hit_rate_window) / len(self.hit_rate_window)

    def record(self, hit: bool):
        self.hit_rate_window.append(hit)
        if len(self.hit_rate_window) > 20:
            self.hit_rate_window.pop(0)
        if hit:
            self.hits += 1
        self.total_specs += 1

    def should_speculate(self, confidence: float) -> bool:
        """Only speculate if expected value (hit_rate * time_saved) > cost."""
        # Simple heuristic: speculate if confidence > 0.3 and hit rate > 0.25
        return confidence > 0.3 and self.hit_rate() > 0.25

budget = SpeculationBudget()
_sem = asyncio.Semaphore(3)
_speculated: dict[str, tuple[asyncio.Task, float]] = {}  # q -> (task, confidence)

async def _speculate(q: str, confidence: float, ctx: list[dict]):
    async with _sem:
        r = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            messages=[*ctx, {"role": "user", "content": q}]
        )
        return r.content[0].text

async def maybe_speculate(predictions: list[tuple[str, float]], ctx: list[dict]):
    """predictions: list of (question, confidence) pairs."""
    for q, conf in sorted(predictions, key=lambda x: -x[1]):
        if q in _speculated:
            continue
        if budget.should_speculate(conf):
            task = asyncio.create_task(_speculate(q, conf, ctx))
            _speculated[q] = (task, conf)

async def answer(user_msg: str, conv: list[dict]) -> str:
    # Check cache
    entry = _speculated.pop(user_msg, None)
    if entry:
        task, conf = entry
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            budget.record(hit=True)
            print(f"[speculation hit, conf={conf:.2f}, hit_rate={budget.hit_rate():.2f}]")
            return result
        except asyncio.TimeoutError:
            budget.record(hit=False)
            _speculated[user_msg] = (task, conf)  # put back

    budget.record(hit=False)
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[*conv, {"role": "user", "content": user_msg}]
    )
    text = resp.content[0].text

    # Get predictions with confidence scores
    pred_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system='Return JSON: [{"q": "...", "confidence": 0.0-1.0}, ...] for top 3 follow-ups.',
        messages=[*conv, {"role": "user", "content": user_msg}, {"role": "assistant", "content": text[:400]},
                  {"role": "user", "content": "Predict follow-ups with confidence."}]
    )
    import json
    try:
        preds_raw = json.loads(pred_resp.content[0].text)
        preds = [(p["q"], float(p["confidence"])) for p in preds_raw]
        new_ctx = [*conv, {"role": "user", "content": user_msg}, {"role": "assistant", "content": text}]
        await maybe_speculate(preds, new_ctx)
    except Exception:
        pass

    return text
```

---

## Comparison

| Option | Prediction Method | Cache Type | Cost Model | Best For |
|--------|-----------------|------------|------------|----------|
| 1 – Haiku Prefetch | LLM-predicted | Dict + Task | Low (Haiku predictor) | General conversations |
| 2 – Semantic Queue | LLM-predicted | LRU Future | Medium | High-traffic chatbots |
| 3 – Template | Regex patterns | Task dict | Very low | Structured domains |
| 4 – Stream Overlap | LLM from partial | Task dict | Medium | Streaming interfaces |
| 5 – Branch Tree | Recursive LLM | Tree nodes | High | Deep exploration |
| 6 – Confidence-Gated | LLM + confidence | Task + budget | Adaptive | Cost-sensitive deployments |

**Recommendation:** Start with Option 1 for general use. Add Option 6's confidence gating when you have usage data and want to control speculation costs. Use Option 3 when your domain has predictable question patterns.
