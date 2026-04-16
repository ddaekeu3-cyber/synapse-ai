---
layout: solution
title: "Agent Doesn't Implement Speculative Execution for Parallel Branches"
category: performance
description: "Agent waits for each decision point before proceeding, adding serial latency when multiple likely branches could be pre-computed in parallel and only the winner kept."
tags: [performance, speculative-execution, parallelism, latency, asyncio]
---

# Agent Doesn't Implement Speculative Execution for Parallel Branches

## Problem

When an agent reaches a conditional decision point — classify the intent, then route to the appropriate handler — it executes sequentially: wait for classification, then execute the winning branch. Speculative execution inverts this: fire all likely branches in parallel speculatively, then discard the losers once the winning condition is known. For branches with similar probability, this trades extra compute for dramatically lower tail latency, often cutting p95 response time by 40-60%.

## Solution Options

### Option 1: Dual-Branch Speculative Execution

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class SpeculativeResult:
    winner: str
    result: str
    total_latency_ms: float
    speculative_latency_ms: float
    tokens_wasted: int

async def classify_intent(user_message: str) -> str:
    """Fast intent classifier — determines winning branch."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8,
        system="Classify intent as exactly one word: 'technical' or 'general'",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip().lower()

async def technical_branch(user_message: str) -> tuple[str, int]:
    """Handler for technical questions."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system="You are a technical expert. Answer with code examples and technical detail.",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text, response.usage.output_tokens

async def general_branch(user_message: str) -> tuple[str, int]:
    """Handler for general questions."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a friendly assistant. Answer conversationally.",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text, response.usage.output_tokens

async def speculative_routing(user_message: str) -> SpeculativeResult:
    """Execute classifier and both branches in parallel; discard loser."""
    t0 = time.monotonic()

    # Launch all three concurrently
    classifier_task = asyncio.create_task(classify_intent(user_message))
    tech_task = asyncio.create_task(technical_branch(user_message))
    general_task = asyncio.create_task(general_branch(user_message))

    # Wait for classifier first
    intent = await classifier_task
    spec_latency = (time.monotonic() - t0) * 1000
    print(f"[CLASSIFIER] intent={intent} at t={spec_latency:.0f}ms")

    # Wait for winning branch; cancel the loser
    if "technical" in intent:
        winner = "technical"
        result, winner_tokens = await tech_task
        if not general_task.done():
            general_task.cancel()
            try:
                _, loser_tokens = await general_task
            except asyncio.CancelledError:
                loser_tokens = 0
        else:
            _, loser_tokens = await general_task
    else:
        winner = "general"
        result, winner_tokens = await general_task
        if not tech_task.done():
            tech_task.cancel()
            try:
                _, loser_tokens = await tech_task
            except asyncio.CancelledError:
                loser_tokens = 0
        else:
            _, loser_tokens = await tech_task

    total_latency = (time.monotonic() - t0) * 1000
    return SpeculativeResult(
        winner=winner,
        result=result,
        total_latency_ms=total_latency,
        speculative_latency_ms=spec_latency,
        tokens_wasted=loser_tokens,
    )

async def sequential_routing(user_message: str) -> tuple[str, float]:
    """Baseline: classify then route (no speculation)."""
    t0 = time.monotonic()
    intent = await classify_intent(user_message)
    if "technical" in intent:
        result, _ = await technical_branch(user_message)
    else:
        result, _ = await general_branch(user_message)
    latency = (time.monotonic() - t0) * 1000
    return result, latency

async def main():
    questions = [
        "How does Python's GIL work and why does it matter for async code?",
        "What's a good book to read this weekend?",
    ]

    for q in questions:
        print(f"\nQ: {q[:60]}...")

        _, seq_latency = await sequential_routing(q)
        spec_result = await speculative_routing(q)

        savings = seq_latency - spec_result.total_latency_ms
        print(f"Sequential: {seq_latency:.0f}ms | Speculative: {spec_result.total_latency_ms:.0f}ms | Savings: {savings:.0f}ms")
        print(f"Winner: {spec_result.winner} | Wasted tokens: {spec_result.tokens_wasted}")

asyncio.run(main())

# Expected Token Savings: Negative (wastes loser branch tokens) but latency savings are 30-60%
# Environment: High-traffic routing agents where latency matters more than token cost
```

### Option 2: Multi-Branch Speculation with Early Termination

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class BranchSpec:
    name: str
    system_prompt: str
    model: str
    prior_probability: float  # 0-1, how likely this branch wins

async def execute_branch(spec: BranchSpec, user_message: str, cancel_event: asyncio.Event) -> tuple[str, str, int]:
    """Execute a branch, checking for cancellation signal."""
    try:
        response = await async_client.messages.create(
            model=spec.model,
            max_tokens=256,
            system=spec.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        if cancel_event.is_set():
            return spec.name, "", response.usage.output_tokens  # Cancelled but completed
        return spec.name, response.content[0].text, response.usage.output_tokens
    except asyncio.CancelledError:
        return spec.name, "", 0

BRANCHES = [
    BranchSpec("code_help", "You are a coding expert. Provide working code.", "claude-sonnet-4-6", 0.4),
    BranchSpec("explanation", "You are a teacher. Explain clearly with analogies.", "claude-haiku-4-5-20251001", 0.35),
    BranchSpec("creative", "You are a creative writer. Be imaginative.", "claude-haiku-4-5-20251001", 0.15),
    BranchSpec("factual", "You are a researcher. Provide facts and citations.", "claude-haiku-4-5-20251001", 0.10),
]

async def classify_branch(user_message: str) -> str:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        system=f"Classify as exactly one: {', '.join(b.name for b in BRANCHES)}",
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text.strip().lower()
    return next((b.name for b in BRANCHES if b.name in text), BRANCHES[0].name)

async def speculative_multi_branch(user_message: str, top_k: int = 2) -> dict:
    """Speculatively execute top-k most likely branches."""
    t0 = time.monotonic()

    # Sort branches by prior probability and speculate on top-k
    sorted_branches = sorted(BRANCHES, key=lambda b: b.prior_probability, reverse=True)
    speculative = sorted_branches[:top_k]
    cancel_event = asyncio.Event()

    print(f"[SPECULATION] Pre-computing branches: {[b.name for b in speculative]}")

    # Launch classifier and speculative branches concurrently
    classifier_task = asyncio.create_task(classify_branch(user_message))
    branch_tasks = {
        b.name: asyncio.create_task(execute_branch(b, user_message, cancel_event))
        for b in speculative
    }

    # Wait for classifier
    winner_name = await classifier_task
    classifier_ms = (time.monotonic() - t0) * 1000
    print(f"[CLASSIFIER] winner={winner_name} at t={classifier_ms:.0f}ms")

    if winner_name in branch_tasks:
        # Winner was pre-computed speculatively
        result_name, result_text, tokens = await branch_tasks[winner_name]
        cancel_event.set()
        hit = True
        # Cancel remaining speculative tasks
        for name, task in branch_tasks.items():
            if name != winner_name:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    else:
        # Speculation miss — execute winning branch now
        cancel_event.set()
        for task in branch_tasks.values():
            task.cancel()
        winner_spec = next(b for b in BRANCHES if b.name == winner_name)
        result_name, result_text, tokens = await execute_branch(winner_spec, user_message, asyncio.Event())
        hit = False

    total_ms = (time.monotonic() - t0) * 1000
    return {
        "winner": winner_name,
        "speculation_hit": hit,
        "result": result_text[:100],
        "total_latency_ms": round(total_ms),
        "classifier_latency_ms": round(classifier_ms),
    }

async def main():
    questions = [
        "Write a Python function to merge two sorted lists.",
        "Explain how black holes form.",
    ]
    for q in questions:
        print(f"\nQ: {q[:60]}...")
        result = await speculative_multi_branch(q, top_k=2)
        print(f"Winner: {result['winner']} | Hit: {result['speculation_hit']} | Total: {result['total_latency_ms']}ms")

asyncio.run(main())

# Expected Token Savings: Trade 1-2 wasted branch executions for ~40% latency reduction
# Environment: Multi-branch routing agents with stable prior probabilities per branch
```

### Option 3: Hedged Requests with First-Response Wins

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class HedgeConfig:
    models: list[str]          # Models to hedge across
    hedge_delay_ms: float      # Delay before launching secondary hedges
    max_hedges: int = 3        # Maximum parallel hedge requests

async def hedged_request(
    user_message: str,
    system: str,
    config: HedgeConfig,
    max_tokens: int = 256,
) -> dict:
    """
    Send request to primary model; if it doesn't respond within hedge_delay_ms,
    launch backup requests to other models. First complete response wins.
    """
    t0 = time.monotonic()
    done_event = asyncio.Event()
    winner_result: dict = {}
    launched: list[str] = []

    async def attempt(model: str, delay_ms: float) -> dict:
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
        if done_event.is_set():
            return {}  # Race already won
        launched.append(model)
        print(f"[HEDGE] Launching {model} at t={int((time.monotonic()-t0)*1000)}ms")
        response = await async_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return {
            "model": model,
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "tokens": response.usage.output_tokens,
        }

    # Launch primary immediately, hedges after delay
    tasks = []
    for i, model in enumerate(config.models[:config.max_hedges]):
        delay = 0 if i == 0 else config.hedge_delay_ms * i
        tasks.append(asyncio.create_task(attempt(model, delay)))

    # Race: take first non-empty result
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result:
            done_event.set()
            winner_result = result
            break

    # Cancel remaining tasks
    for task in tasks:
        task.cancel()

    winner_result["models_launched"] = launched
    winner_result["total_latency_ms"] = int((time.monotonic() - t0) * 1000)
    return winner_result

async def main():
    config = HedgeConfig(
        models=["claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"],
        hedge_delay_ms=500,  # Launch backup after 500ms
        max_hedges=3,
    )

    questions = [
        "What is dependency injection?",
        "Explain the CAP theorem briefly.",
    ]

    for q in questions:
        print(f"\nQ: {q}")
        result = await hedged_request(q, "You are a helpful assistant.", config)
        print(f"Winner: {result['model']} | Latency: {result['total_latency_ms']}ms | Launched: {result['models_launched']}")
        print(f"Response: {result['text'][:80]}...")

asyncio.run(main())

# Expected Token Savings: Negative on hedge hits but reduces p95/p99 latency significantly
# Environment: Latency-sensitive applications where tail latency is more costly than extra tokens
```

### Option 4: Conditional Speculation Based on Confidence

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class ConfidenceClassification:
    intent: str
    confidence: float  # 0.0 - 1.0

async def classify_with_confidence(user_message: str) -> ConfidenceClassification:
    """Classify and return confidence score."""
    import json
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system='Return JSON only: {"intent": "technical|general|creative", "confidence": 0.0-1.0}',
        messages=[{"role": "user", "content": user_message}],
    )
    try:
        data = json.loads(response.content[0].text.strip())
        return ConfidenceClassification(
            intent=data.get("intent", "general"),
            confidence=float(data.get("confidence", 0.5)),
        )
    except Exception:
        return ConfidenceClassification(intent="general", confidence=0.5)

async def handle_intent(intent: str, user_message: str) -> tuple[str, int]:
    system_map = {
        "technical": "You are a technical expert. Be precise and detailed.",
        "general": "You are a helpful assistant. Be conversational.",
        "creative": "You are a creative writer. Be imaginative.",
    }
    system = system_map.get(intent, system_map["general"])
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text, response.usage.output_tokens

SPECULATION_THRESHOLD = 0.6  # Only speculate when confidence < 60% (high uncertainty)

async def confidence_gated_speculation(user_message: str) -> dict:
    """
    Speculate only when classifier confidence is low.
    High confidence → sequential (no waste).
    Low confidence → parallel speculation (reduce latency).
    """
    t0 = time.monotonic()

    # Start classifier and speculatively start all branches
    classifier_task = asyncio.create_task(classify_with_confidence(user_message))
    speculative_tasks = {
        intent: asyncio.create_task(handle_intent(intent, user_message))
        for intent in ["technical", "general", "creative"]
    }

    clf = await classifier_task
    clf_ms = int((time.monotonic() - t0) * 1000)
    print(f"[CLASSIFIER] intent={clf.intent} confidence={clf.confidence:.2f} at t={clf_ms}ms")

    mode = "speculative" if clf.confidence < SPECULATION_THRESHOLD else "sequential"
    print(f"[MODE] {mode} (threshold={SPECULATION_THRESHOLD})")

    if mode == "sequential":
        # Cancel all speculative tasks — we're confident enough
        for task in speculative_tasks.values():
            task.cancel()
        result, tokens = await handle_intent(clf.intent, user_message)
        wasted_tokens = 0
    else:
        # Use pre-computed speculative result
        result, tokens = await speculative_tasks[clf.intent]
        wasted_tokens = 0
        for intent, task in speculative_tasks.items():
            if intent != clf.intent:
                task.cancel()
                try:
                    _, t = await task
                    wasted_tokens += t
                except (asyncio.CancelledError, Exception):
                    pass

    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "intent": clf.intent,
        "confidence": clf.confidence,
        "mode": mode,
        "result_preview": result[:80],
        "total_latency_ms": total_ms,
        "wasted_tokens": wasted_tokens,
    }

async def main():
    queries = [
        "How do I write a Python decorator?",
        "Tell me something interesting.",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        result = await confidence_gated_speculation(q)
        print(json.dumps({k: v for k, v in result.items() if k != "result_preview"}, indent=2))
        print(f"Preview: {result['result_preview']}...")

import json
asyncio.run(main())

# Expected Token Savings: Adaptive — only wastes on low-confidence inputs; saves tokens on high-confidence
# Environment: Agents with variable query ambiguity where wasting tokens on clear cases is unacceptable
```

### Option 5: Speculative Tool Pre-fetching

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class ToolPrefetchResult:
    tool_name: str
    tool_input: dict
    result: dict
    latency_ms: float
    used: bool = False

async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Simulate tool execution with latency."""
    await asyncio.sleep(0.05)  # Simulate 50ms tool latency
    if tool_name == "get_user_profile":
        return {"id": tool_input.get("user_id"), "name": "Alice", "plan": "pro"}
    elif tool_name == "get_account_balance":
        return {"user_id": tool_input.get("user_id"), "balance": 1250.00}
    elif tool_name == "get_recent_activity":
        return {"user_id": tool_input.get("user_id"), "events": ["login", "purchase"]}
    return {"status": "ok"}

async def speculative_tool_prefetch(
    user_message: str,
    predicted_tools: list[tuple[str, dict]],  # (tool_name, args) pairs to pre-fetch
) -> dict:
    """
    Pre-fetch predicted tool results in parallel with the LLM's first turn.
    If the LLM requests a pre-fetched tool, serve from cache instantly.
    """
    t0 = time.monotonic()

    tools_schema = [
        {"name": "get_user_profile", "description": "Get user profile", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
        {"name": "get_account_balance", "description": "Get account balance", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
        {"name": "get_recent_activity", "description": "Get recent activity", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    ]

    # Pre-fetch predicted tools concurrently
    prefetch_tasks = {
        f"{name}:{json.dumps(args, sort_keys=True)}": asyncio.create_task(execute_tool(name, args))
        for name, args in predicted_tools
    }

    # First LLM turn (concurrent with prefetch)
    llm_task = asyncio.create_task(async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools_schema,
        messages=[{"role": "user", "content": user_message}],
    ))

    response = await llm_task
    llm_ms = int((time.monotonic() - t0) * 1000)
    print(f"[LLM] First response at t={llm_ms}ms")

    prefetch_cache: dict[str, dict] = {}
    for key, task in prefetch_tasks.items():
        if task.done():
            prefetch_cache[key] = task.result()
        else:
            task.cancel()

    # Serve tool calls from cache if available
    cache_hits = 0
    cache_misses = 0
    tool_results = []
    messages = [{"role": "user", "content": user_message}]

    if response.stop_reason == "tool_use":
        for block in response.content:
            if block.type == "tool_use":
                cache_key = f"{block.name}:{json.dumps(block.input, sort_keys=True)}"
                if cache_key in prefetch_cache:
                    result = prefetch_cache[cache_key]
                    cache_hits += 1
                    print(f"[PREFETCH HIT] {block.name} served from cache")
                else:
                    result = await execute_tool(block.name, block.input)
                    cache_misses += 1
                    print(f"[PREFETCH MISS] {block.name} executed live")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools_schema,
            messages=messages,
        )

    total_ms = int((time.monotonic() - t0) * 1000)
    final_text = next((b.text for b in response.content if hasattr(b, "text")), "")
    return {
        "total_latency_ms": total_ms,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "result": final_text[:100],
    }

async def main():
    # Predict user u42 will likely trigger profile+balance lookups
    result = await speculative_tool_prefetch(
        "Show me a summary of user u42's account status.",
        predicted_tools=[
            ("get_user_profile", {"user_id": "u42"}),
            ("get_account_balance", {"user_id": "u42"}),
        ],
    )
    print(f"\nResult: {json.dumps(result, indent=2)}")

asyncio.run(main())

# Expected Token Savings: None; tool pre-fetching eliminates tool latency (50-200ms per tool)
# Environment: Agents with predictable tool access patterns (e.g. always fetch user profile first)
```

### Option 6: Speculative Context Preparation

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class ContextBundle:
    """Pre-computed context that may be needed for a response."""
    name: str
    content: str
    tokens: int
    relevant: bool = False

async def fetch_context_bundle(bundle_name: str, query: str) -> ContextBundle:
    """Simulate fetching a context bundle (e.g., from RAG, DB, or API)."""
    await asyncio.sleep(0.03)  # Simulate 30ms fetch latency

    bundles = {
        "user_history": f"User has asked about Python, async, and testing previously.",
        "docs_context": f"Relevant docs: asyncio.gather(), asyncio.create_task(), asyncio.TaskGroup.",
        "code_examples": "Example: async def fetch(): return await client.get('/api')",
        "faq_context": "Common FAQ: How to handle exceptions in async code.",
    }
    content = bundles.get(bundle_name, f"Context for {bundle_name}")
    return ContextBundle(name=bundle_name, content=content, tokens=len(content) // 4)

async def classify_needed_context(user_message: str) -> list[str]:
    """Classify which context bundles are needed."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system='Return JSON array of needed context bundles. Options: ["user_history", "docs_context", "code_examples", "faq_context"]. Return only what is needed.',
        messages=[{"role": "user", "content": user_message}],
    )
    try:
        text = response.content[0].text.strip()
        if "[" in text:
            text = text[text.index("["):text.rindex("]")+1]
        return json.loads(text)
    except Exception:
        return ["docs_context"]

async def speculative_context_preparation(user_message: str, top_k_bundles: int = 2) -> dict:
    """
    Pre-fetch top-k most likely context bundles speculatively.
    Then determine which are actually needed, discard the rest.
    """
    t0 = time.monotonic()

    # Always fetch these high-probability bundles speculatively
    speculative_bundles = ["user_history", "docs_context"][:top_k_bundles]

    prefetch_tasks = {
        name: asyncio.create_task(fetch_context_bundle(name, user_message))
        for name in speculative_bundles
    }
    classifier_task = asyncio.create_task(classify_needed_context(user_message))

    needed_bundles = await classifier_task
    clf_ms = int((time.monotonic() - t0) * 1000)
    print(f"[CLASSIFIER] needs={needed_bundles} at t={clf_ms}ms")

    # Collect pre-fetched bundles, fetch any missed ones
    context_parts = []
    hits = misses = wasted = 0

    for bundle_name in needed_bundles:
        if bundle_name in prefetch_tasks:
            bundle = await prefetch_tasks[bundle_name]
            bundle.relevant = True
            context_parts.append(bundle.content)
            hits += 1
        else:
            bundle = await fetch_context_bundle(bundle_name, user_message)
            context_parts.append(bundle.content)
            misses += 1

    # Cancel unused prefetch tasks
    for name, task in prefetch_tasks.items():
        if name not in needed_bundles:
            if task.done():
                wasted += 1
            else:
                task.cancel()

    context_str = "\n\n".join(context_parts) if context_parts else "No additional context."

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nContext:\n{context_str}",
        messages=[{"role": "user", "content": user_message}],
    )

    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "total_latency_ms": total_ms,
        "context_cache_hits": hits,
        "context_cache_misses": misses,
        "wasted_prefetches": wasted,
        "result": response.content[0].text[:100],
    }

async def main():
    queries = [
        "How do I handle exceptions in asyncio tasks?",
        "What's the best way to learn programming?",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        result = await speculative_context_preparation(q, top_k_bundles=2)
        print(json.dumps({k: v for k, v in result.items() if k != "result"}, indent=2))
        print(f"Preview: {result['result']}...")

asyncio.run(main())

# Expected Token Savings: Context fetch latency eliminated for pre-fetched bundles (30-150ms)
# Environment: RAG agents where context retrieval latency is a bottleneck
```

## Comparison

| Option | Speculation Type | Cancellable | Confidence-Gated | Token Cost | Latency Savings |
|--------|-----------------|------------|-----------------|-----------|----------------|
| 1. Dual-Branch | Full response | Yes | No | 1 wasted branch | 30-50% |
| 2. Multi-Branch Top-K | Full response | Yes | No | K-1 wasted branches | 40-60% |
| 3. Hedged Requests | Full response (multi-model) | Yes | No | 1-2 wasted responses | p99: 50-70% |
| 4. Confidence-Gated | Full response | Yes | Yes | Adaptive | 20-50% on uncertain |
| 5. Tool Pre-fetching | Tool execution only | Partial | No | ~0 (tool calls free) | 50-80% tool latency |
| 6. Context Pre-fetch | Context retrieval only | Yes | Implicit | ~0 | 30-60% RAG latency |
