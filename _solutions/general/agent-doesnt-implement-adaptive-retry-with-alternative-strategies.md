---
title: "Agent Doesn't Implement Adaptive Retry with Alternative Strategies"
description: "When a request fails, don't just retry the same prompt — adapt the strategy: simplify the prompt, switch models, decompose the task, or change approach entirely."
category: general
difficulty: intermediate
tags: [retry, resilience, fallback, adaptive, reliability, prompt-engineering]
---

# Agent Doesn't Implement Adaptive Retry with Alternative Strategies

## Problem

Naive retry logic sends the identical failing request again and again. If the prompt was too complex, the model will fail again. If the response format was wrong, the same prompt will produce the same bad format. Adaptive retry changes the strategy on each attempt: simplify, rephrase, decompose, switch models, or fall back to a different approach.

---

## Option 1: Strategy Ladder with Per-Attempt Transformation

```python
import asyncio
import anthropic
from dataclasses import dataclass
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class Strategy:
    name: str
    transform: Callable[[str, str], tuple[str, str]]  # (prompt, system) -> (new_prompt, new_system)
    model: str
    max_tokens: int

STRATEGIES = [
    Strategy(
        name="direct",
        transform=lambda p, s: (p, s),
        model="claude-sonnet-4-6",
        max_tokens=1024,
    ),
    Strategy(
        name="simplified",
        transform=lambda p, s: (f"Please answer this simply and concisely:\n{p}", "Be brief and direct."),
        model="claude-sonnet-4-6",
        max_tokens=512,
    ),
    Strategy(
        name="step-by-step",
        transform=lambda p, s: (f"Think step by step, then answer:\n{p}", "Work through this carefully."),
        model="claude-sonnet-4-6",
        max_tokens=1024,
    ),
    Strategy(
        name="haiku-fallback",
        transform=lambda p, s: (p, "Be concise."),
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
    ),
    Strategy(
        name="minimal",
        transform=lambda p, s: (f"In one sentence: {p}", ""),
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
    ),
]

async def adaptive_call(
    prompt: str,
    system: str = "",
    validator: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    """Returns (response_text, strategy_name_used)."""
    last_error: Exception | None = None

    for strategy in STRATEGIES:
        adapted_prompt, adapted_system = strategy.transform(prompt, system)
        try:
            msgs = [{"role": "user", "content": adapted_prompt}]
            kwargs = {"model": strategy.model, "max_tokens": strategy.max_tokens, "messages": msgs}
            if adapted_system:
                kwargs["system"] = adapted_system

            resp = await asyncio.wait_for(
                client.messages.create(**kwargs),
                timeout=15.0
            )
            text = resp.content[0].text

            # Optional validation — if fails, try next strategy
            if validator and not validator(text):
                print(f"[ADAPTIVE] Strategy '{strategy.name}' failed validation, trying next")
                continue

            if strategy.name != "direct":
                print(f"[ADAPTIVE] Succeeded with strategy: {strategy.name}")
            return text, strategy.name

        except asyncio.TimeoutError:
            print(f"[ADAPTIVE] Strategy '{strategy.name}' timed out")
            last_error = TimeoutError(f"{strategy.name} timed out")
        except anthropic.APIStatusError as e:
            print(f"[ADAPTIVE] Strategy '{strategy.name}' API error: {e.status_code}")
            last_error = e
        except Exception as e:
            print(f"[ADAPTIVE] Strategy '{strategy.name}' failed: {e}")
            last_error = e

    raise RuntimeError(f"All strategies exhausted. Last error: {last_error}")

async def main():
    # Example: require JSON in response
    def is_json(text: str) -> bool:
        import json
        try:
            json.loads(text)
            return True
        except Exception:
            return False

    # Normal call
    result, strategy = await adaptive_call("What is the capital of France?")
    print(f"[{strategy}] {result}")

    # Call requiring JSON — will escalate strategies until one produces valid JSON
    json_result, strategy = await adaptive_call(
        'Return {"country": "France", "capital": "..."} with the capital filled in.',
        validator=is_json
    )
    print(f"[{strategy}] {json_result}")

asyncio.run(main())
```

---

## Option 2: Error-Classified Retry with Targeted Recovery

```python
import asyncio
import anthropic
import json
import re
from enum import Enum
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

class FailureType(Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    BAD_FORMAT = "bad_format"
    CONTEXT_TOO_LONG = "context_too_long"
    UNKNOWN = "unknown"

def classify_failure(error: Exception | None, response: str | None) -> FailureType:
    if isinstance(error, asyncio.TimeoutError):
        return FailureType.TIMEOUT
    if isinstance(error, anthropic.RateLimitError):
        return FailureType.RATE_LIMIT
    if isinstance(error, anthropic.APIStatusError):
        if error.status_code == 529:
            return FailureType.OVERLOADED
        if "context" in str(error).lower() or "too long" in str(error).lower():
            return FailureType.CONTEXT_TOO_LONG
    if response is not None:
        return FailureType.BAD_FORMAT
    return FailureType.UNKNOWN

@dataclass
class RecoveryAction:
    failure: FailureType
    wait_seconds: float
    model_override: str | None
    prompt_transform: callable
    max_tokens_override: int | None = None

RECOVERY_ACTIONS: dict[FailureType, RecoveryAction] = {
    FailureType.TIMEOUT: RecoveryAction(
        failure=FailureType.TIMEOUT,
        wait_seconds=1.0,
        model_override="claude-haiku-4-5-20251001",
        prompt_transform=lambda p: f"Answer briefly: {p}",
        max_tokens_override=256,
    ),
    FailureType.RATE_LIMIT: RecoveryAction(
        failure=FailureType.RATE_LIMIT,
        wait_seconds=5.0,
        model_override=None,
        prompt_transform=lambda p: p,
    ),
    FailureType.OVERLOADED: RecoveryAction(
        failure=FailureType.OVERLOADED,
        wait_seconds=3.0,
        model_override="claude-haiku-4-5-20251001",
        prompt_transform=lambda p: p,
    ),
    FailureType.BAD_FORMAT: RecoveryAction(
        failure=FailureType.BAD_FORMAT,
        wait_seconds=0.0,
        model_override=None,
        prompt_transform=lambda p: p + "\n\nIMPORTANT: Respond with valid JSON only. No other text.",
    ),
    FailureType.CONTEXT_TOO_LONG: RecoveryAction(
        failure=FailureType.CONTEXT_TOO_LONG,
        wait_seconds=0.0,
        model_override=None,
        prompt_transform=lambda p: p[:len(p)//2] + "\n[truncated for length]",
        max_tokens_override=512,
    ),
    FailureType.UNKNOWN: RecoveryAction(
        failure=FailureType.UNKNOWN,
        wait_seconds=2.0,
        model_override="claude-haiku-4-5-20251001",
        prompt_transform=lambda p: p,
    ),
}

async def resilient_call(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    validator: callable | None = None,
    max_attempts: int = 4,
) -> str:
    current_prompt = prompt
    current_model = model
    current_max_tokens = max_tokens

    for attempt in range(max_attempts):
        error: Exception | None = None
        response_text: str | None = None
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=current_model,
                    max_tokens=current_max_tokens,
                    messages=[{"role": "user", "content": current_prompt}]
                ),
                timeout=12.0
            )
            response_text = resp.content[0].text
            if validator and not validator(response_text):
                error = None  # format failure, not API failure
            else:
                return response_text
        except asyncio.TimeoutError as e:
            error = e
        except anthropic.APIStatusError as e:
            error = e
        except Exception as e:
            error = e

        if attempt == max_attempts - 1:
            break

        failure_type = classify_failure(error, response_text if (validator and not validator(response_text or "")) else None)
        action = RECOVERY_ACTIONS.get(failure_type, RECOVERY_ACTIONS[FailureType.UNKNOWN])
        print(f"[ADAPTIVE] Attempt {attempt+1} failed ({failure_type.value}). Applying recovery: wait={action.wait_seconds}s")

        if action.wait_seconds > 0:
            await asyncio.sleep(action.wait_seconds)
        if action.model_override:
            current_model = action.model_override
        if action.max_tokens_override:
            current_max_tokens = action.max_tokens_override
        current_prompt = action.prompt_transform(current_prompt)

    # Last attempt with whatever state we have
    resp = await client.messages.create(
        model=current_model, max_tokens=current_max_tokens,
        messages=[{"role": "user", "content": current_prompt}]
    )
    return resp.content[0].text

async def main():
    def needs_json(text: str) -> bool:
        try:
            json.loads(text)
            return True
        except Exception:
            return False

    result = await resilient_call(
        'List 3 Python features as {"features": ["...", "...", "..."]}',
        validator=needs_json
    )
    print(result)

asyncio.run(main())
```

---

## Option 3: Task Decomposition on Complexity Failure

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic()

async def decompose_task(prompt: str) -> list[str]:
    """Break a complex task into simpler subtasks."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system='Break this task into 3-5 simpler, sequential subtasks. Return as JSON array of strings. Each subtask should be independently answerable.',
        messages=[{"role": "user", "content": f"Task: {prompt}"}]
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return [prompt]  # fallback: treat as single task

async def execute_subtask(subtask: str, context: str = "") -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"Context from previous steps:\n{context}" if context else "Answer concisely.",
        messages=[{"role": "user", "content": subtask}]
    )
    return resp.content[0].text

async def synthesize_results(original_prompt: str, subtask_results: list[tuple[str, str]]) -> str:
    parts = "\n\n".join([f"Subtask: {st}\nResult: {res}" for st, res in subtask_results])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system="Synthesize the subtask results into a coherent, complete answer to the original question.",
        messages=[{"role": "user", "content": f"Original question: {original_prompt}\n\nSubtask results:\n{parts}"}]
    )
    return resp.content[0].text

async def adaptive_decompose_call(prompt: str, complexity_threshold: int = 300) -> str:
    """Try direct answer first; decompose if prompt is complex or direct answer fails."""
    # Attempt direct answer for simple prompts
    if len(prompt) < complexity_threshold:
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}]
                ),
                timeout=12.0
            )
            return resp.content[0].text
        except Exception as e:
            print(f"[DECOMPOSE] Direct call failed ({e}), decomposing task")

    # Decompose and solve
    print("[DECOMPOSE] Breaking task into subtasks")
    subtasks = await decompose_task(prompt)
    print(f"[DECOMPOSE] {len(subtasks)} subtasks: {subtasks}")

    results: list[tuple[str, str]] = []
    accumulated_context = ""
    for subtask in subtasks:
        result = await execute_subtask(subtask, accumulated_context)
        results.append((subtask, result))
        accumulated_context += f"\n{subtask}: {result[:200]}"

    return await synthesize_results(prompt, results)

async def main():
    complex_prompt = """
    Explain: (1) what asyncio is, (2) how the event loop works,
    (3) the difference between coroutines and threads,
    (4) when to use asyncio vs multiprocessing,
    (5) common asyncio pitfalls and how to avoid them.
    """
    result = await adaptive_decompose_call(complex_prompt.strip())
    print(result[:400])

asyncio.run(main())
```

---

## Option 4: Multi-Model Consensus with Fallback Confidence

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class ModelAttempt:
    model: str
    response: str
    confidence: float

async def try_model(prompt: str, model: str, max_tokens: int) -> ModelAttempt | None:
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            ),
            timeout=10.0
        )
        return ModelAttempt(model=model, response=resp.content[0].text, confidence=1.0)
    except Exception as e:
        print(f"[CONSENSUS] {model} failed: {e}")
        return None

async def score_response(prompt: str, response: str) -> float:
    """Use Haiku to score response quality (0.0-1.0)."""
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system="Rate the response quality 0.0-1.0. Return only the decimal number.",
            messages=[{"role": "user", "content": f"Question: {prompt}\nResponse: {response[:300]}"}]
        )
        return float(resp.content[0].text.strip())
    except Exception:
        return 0.5

async def adaptive_consensus_call(prompt: str, min_confidence: float = 0.7) -> str:
    # Try primary model first
    primary = await try_model(prompt, "claude-sonnet-4-6", 1024)
    if primary:
        confidence = await score_response(prompt, primary.response)
        if confidence >= min_confidence:
            return primary.response
        print(f"[CONSENSUS] Primary confidence {confidence:.2f} < {min_confidence}, trying alternatives")

    # Try alternatives in parallel
    attempts = await asyncio.gather(
        try_model(prompt, "claude-haiku-4-5-20251001", 512),
        try_model(f"Rephrase and answer: {prompt}", "claude-sonnet-4-6", 1024),
        try_model(f"Step by step: {prompt}", "claude-haiku-4-5-20251001", 512),
    )
    valid = [a for a in attempts if a is not None]
    if not valid:
        raise RuntimeError("All model attempts failed")

    # Score all valid attempts in parallel
    scores = await asyncio.gather(*[score_response(prompt, a.response) for a in valid])
    for attempt, score in zip(valid, scores):
        attempt.confidence = score

    # Pick highest confidence
    best = max(zip(valid, scores), key=lambda x: x[1])
    print(f"[CONSENSUS] Best response from {best[0].model} (confidence={best[1]:.2f})")

    # If still below threshold, synthesize
    if best[1] < min_confidence and len(valid) >= 2:
        candidates = "\n\n".join([f"Option {i+1}: {a.response[:200]}" for i, a in enumerate(valid[:3])])
        synth = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            system="Synthesize the best elements from these candidate responses into one optimal answer.",
            messages=[{"role": "user", "content": f"Question: {prompt}\n\nCandidates:\n{candidates}"}]
        )
        return synth.content[0].text

    return best[0].response

async def main():
    result = await adaptive_consensus_call("What are the key differences between SQL and NoSQL databases?")
    print(result[:300])

asyncio.run(main())
```

---

## Option 5: Format-Repair Retry Loop

```python
import asyncio
import anthropic
import json
import re
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class FormatRepairStrategy:
    name: str
    repair_prompt: str
    max_tokens: int

FORMAT_REPAIRS = [
    FormatRepairStrategy(
        name="explicit_json",
        repair_prompt="The previous response was not valid JSON. Respond with ONLY a valid JSON object. No markdown, no explanation, just the raw JSON.",
        max_tokens=512,
    ),
    FormatRepairStrategy(
        name="extract_json",
        repair_prompt="Extract and return ONLY the JSON from your previous response. Remove all surrounding text.",
        max_tokens=256,
    ),
    FormatRepairStrategy(
        name="reconstruct",
        repair_prompt="Reconstruct your answer as a clean JSON object with the same information. Start your response with { and end with }.",
        max_tokens=512,
    ),
]

def try_extract_json(text: str) -> Any | None:
    """Try multiple JSON extraction strategies."""
    # Try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try extracting from code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try finding first { ... }
    m = re.search(r"(\{[^{}]*\})", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None

async def call_with_format_repair(
    prompt: str,
    system: str = "",
    expected_format: str = "json",
    max_repair_attempts: int = 3,
) -> tuple[str, bool]:
    """Returns (response, successfully_parsed)."""
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": "claude-sonnet-4-6", "max_tokens": 1024, "messages": messages}
    if system:
        kwargs["system"] = system

    resp = await client.messages.create(**kwargs)
    text = resp.content[0].text

    if expected_format != "json":
        return text, True

    parsed = try_extract_json(text)
    if parsed is not None:
        return json.dumps(parsed), True

    # Repair loop
    repair_messages = messages + [
        {"role": "assistant", "content": text}
    ]

    for i, repair in enumerate(FORMAT_REPAIRS[:max_repair_attempts]):
        print(f"[FORMAT REPAIR] Attempt {i+1}: {repair.name}")
        repair_messages = repair_messages + [{"role": "user", "content": repair.repair_prompt}]
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=repair.max_tokens,
            messages=repair_messages
        )
        repaired = resp.content[0].text
        parsed = try_extract_json(repaired)
        if parsed is not None:
            print(f"[FORMAT REPAIR] Succeeded with: {repair.name}")
            return json.dumps(parsed), True
        repair_messages = repair_messages + [{"role": "assistant", "content": repaired}]

    return text, False

async def main():
    result, success = await call_with_format_repair(
        'List 3 programming languages with their primary use case as {"languages": [{"name": "...", "use_case": "..."}]}',
        expected_format="json"
    )
    print(f"Success: {success}\nResult: {result}")

asyncio.run(main())
```

---

## Option 6: Context-Aware Strategy Selection via Meta-Reasoning

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveStrategy:
    name: str
    description: str
    execute: Callable[[str, dict], Awaitable[str]]

async def _direct(prompt: str, ctx: dict) -> str:
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

async def _chain_of_thought(prompt: str, ctx: dict) -> str:
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2048,
        system="Think step by step before answering.",
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

async def _simplify(prompt: str, ctx: dict) -> str:
    simp_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=100,
        system="Rephrase this question more simply in one sentence.",
        messages=[{"role": "user", "content": prompt}]
    )
    simplified = simp_resp.content[0].text
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{"role": "user", "content": simplified}]
    )
    return resp.content[0].text

async def _haiku_fast(prompt: str, ctx: dict) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

STRATEGIES = [
    AdaptiveStrategy("direct", "Standard call", _direct),
    AdaptiveStrategy("chain_of_thought", "Step-by-step reasoning", _chain_of_thought),
    AdaptiveStrategy("simplify", "Simplify then answer", _simplify),
    AdaptiveStrategy("haiku_fast", "Fast small model", _haiku_fast),
]

async def select_strategy(prompt: str, previous_failures: list[str]) -> str:
    failure_ctx = f"Previously tried: {previous_failures}" if previous_failures else "No prior attempts."
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system=f'Select the best strategy for this prompt from: {[s.name for s in STRATEGIES]}. {failure_ctx}\nReturn only the strategy name.',
        messages=[{"role": "user", "content": prompt[:300]}]
    )
    chosen = resp.content[0].text.strip().lower()
    # Validate against known strategies
    for s in STRATEGIES:
        if s.name in chosen:
            return s.name
    return "direct"

async def meta_adaptive_call(prompt: str, validator: Callable[[str], bool] | None = None) -> str:
    tried: list[str] = []
    strategy_map = {s.name: s for s in STRATEGIES}

    for attempt in range(len(STRATEGIES)):
        strategy_name = await select_strategy(prompt, tried)
        if strategy_name in tried:
            # Pick any untried strategy
            untried = [s.name for s in STRATEGIES if s.name not in tried]
            if not untried:
                break
            strategy_name = untried[0]

        strategy = strategy_map.get(strategy_name, STRATEGIES[0])
        tried.append(strategy_name)

        try:
            result = await asyncio.wait_for(strategy.execute(prompt, {}), timeout=15.0)
            if validator and not validator(result):
                print(f"[META] '{strategy_name}' failed validation")
                continue
            if strategy_name != "direct":
                print(f"[META] Succeeded with strategy: {strategy_name}")
            return result
        except Exception as e:
            print(f"[META] '{strategy_name}' error: {e}")

    raise RuntimeError(f"All strategies failed. Tried: {tried}")

async def main():
    result = await meta_adaptive_call(
        "Explain the CAP theorem and its implications for distributed database design.",
    )
    print(result[:300])

asyncio.run(main())
```

---

## Comparison

| Option | Trigger | Strategy Change | Model Switch | Best For |
|--------|---------|----------------|-------------|---------|
| 1 – Strategy Ladder | Any failure or validation miss | Fixed ladder of transforms | Yes (Haiku fallback) | General-purpose resilience |
| 2 – Error-Classified | Exception type or format error | Targeted recovery per error class | Yes | Production agents with diverse failure modes |
| 3 – Task Decomposition | Complexity or direct failure | Split into subtasks | No (Haiku for subtasks) | Complex multi-part requests |
| 4 – Multi-Model Consensus | Low confidence score | Parallel alternatives + synthesis | Yes | High-stakes accuracy requirements |
| 5 – Format-Repair | JSON parse failure | Escalating repair prompts | No | Structured output agents |
| 6 – Meta-Reasoning | Any failure or validation | LLM selects next strategy | Yes | Agents with unpredictable failure patterns |

**Recommendation:** Use Option 2 (error-classified) as your foundation — it handles the most common failure modes with targeted recovery. Layer Option 5 (format-repair) on top for any agent that depends on structured JSON output. Add Option 3's decomposition for complex analytical tasks that regularly exceed the model's single-turn capacity.
