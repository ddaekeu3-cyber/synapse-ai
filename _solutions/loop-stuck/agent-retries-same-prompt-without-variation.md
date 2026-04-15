---
layout: solution
title: "Agent retries same prompt without variation"
category: loop-stuck
description: "When a tool call fails or the model produces an unusable response, the agent retries with the exact same prompt and parameters. Because the model is deterministic at low temperature, it produces the same wrong output every time — burning tokens in a tight loop."
tags: [loop-stuck, retry, prompt-engineering, temperature, debugging]
---

## Symptom

The agent hits an error or produces a bad output, then retries identically 3–5 times, getting the same wrong result each time. The loop eventually exhausts its retry budget without making progress. Logs show the same tool arguments, the same model output, and the same error repeated verbatim.

## Root Cause

Retry logic copies the previous request unchanged. At low temperature (0.0–0.3), the model is nearly deterministic — the same prompt produces the same tokens. Without variation — a different instruction, a concrete error message, a higher temperature, or a reformulated question — each retry is guaranteed to fail the same way.

## Fix

Every retry must change something: inject the error message, rephrase the instruction, increase temperature, or try a fallback strategy. "Retry with variation" is the minimum viable loop-breaking mechanism.

---

### Option 1 — Inject error message on retry

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_error_injection(
    user_message: str,
    max_attempts: int = 3,
) -> str:
    """On failure, tell the model exactly what went wrong and ask it to try again."""
    messages: list[dict] = [{"role": "user", "content": user_message}]
    last_error: str | None = None

    for attempt in range(max_attempts):
        if last_error and attempt > 0:
            # Inject the error into the conversation before retrying
            messages.append({"role": "assistant", "content": messages[-1].get("content", "")})
            messages.append({
                "role": "user",
                "content": (
                    f"That didn't work. Error: {last_error}\n\n"
                    f"Please try a different approach."
                ),
            })

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text

        # Simulate validation
        if "```python" in output and "import" in output:
            return output   # success

        last_error = f"Output missing required Python code block (got: {output[:80]}...)"

    raise RuntimeError(f"Failed after {max_attempts} attempts. Last error: {last_error}")
```

**Expected Token Savings:** Compared to retrying without context: each corrective turn resolves the issue in 1–2 retries instead of exhausting 5; ~60 % fewer total retry tokens.
**Environment:** Any agent that validates its own output; the error message is the minimum variation needed to break the loop.

---

### Option 2 — Temperature escalation on retry

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Temperature schedule: each retry is more exploratory
TEMPERATURE_SCHEDULE = [0.0, 0.4, 0.8, 1.0]


def run_with_temperature_escalation(
    user_message: str,
    validator: callable,
) -> str:
    """Increase temperature on each retry to escape deterministic failure modes."""
    for attempt, temp in enumerate(TEMPERATURE_SCHEDULE):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            temperature=temp,
            messages=[{"role": "user", "content": user_message}],
        )
        output = response.content[0].text

        try:
            result = validator(output)
            if attempt > 0:
                print(f"Succeeded on attempt {attempt + 1} (temperature={temp})")
            return result
        except ValueError as exc:
            print(f"Attempt {attempt + 1} failed at temp={temp}: {exc}")
            if attempt == len(TEMPERATURE_SCHEDULE) - 1:
                raise RuntimeError(f"All temperature levels exhausted. Last error: {exc}")

    return ""


def validate_json_output(text: str) -> dict:
    import json
    import re
    # Extract JSON from code block or raw
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = match.group(1) if match else text.strip()
    return json.loads(raw)


# Usage
result = run_with_temperature_escalation(
    "Return a JSON object with keys: name, score, rank.",
    validate_json_output,
)
```

**Expected Token Savings:** Temperature 0 fails deterministically; escalating breaks the deadlock without adding corrective turns.
**Environment:** Any task where the model produces consistently malformed output at low temperature; especially useful for structured output generation.

---

### Option 3 — Prompt reformulation on retry

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

REPHRASING_PROMPTS = [
    # Attempt 0: original
    "{original}",
    # Attempt 1: add explicit format constraint
    "{original}\n\nIMPORTANT: Respond with ONLY a valid JSON object. No prose, no markdown fences.",
    # Attempt 2: few-shot example
    "{original}\n\nExample of the expected format:\n{{\"name\": \"example\", \"value\": 42}}\n\nNow provide the actual answer in the same format.",
    # Attempt 3: decompose the request
    "First, identify the key information needed for: {original}\nThen provide only that information as a JSON object.",
]


def run_with_reformulation(user_message: str) -> str:
    import json

    for attempt, template in enumerate(REPHRASING_PROMPTS):
        prompt = template.format(original=user_message)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        output = response.content[0].text.strip()

        # Try to parse as JSON
        try:
            # Strip code fences if present
            clean = output.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if attempt > 0:
                print(f"Succeeded with reformulation #{attempt}")
            return output
        except json.JSONDecodeError:
            print(f"Reformulation {attempt} failed: not valid JSON")

    raise RuntimeError("All reformulations exhausted")
```

**Expected Token Savings:** Each reformulation adds ~50–100 tokens but breaks the loop in 1–2 retries rather than 5; net saving vs. 5 identical failed attempts.
**Environment:** Structured output tasks (JSON, CSV, code) where the model understands the task but formats incorrectly.

---

### Option 4 — Strategy rotation: cycle through multiple approaches

```python
import anthropic
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")


def strategy_direct(question: str) -> str:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text


def strategy_chain_of_thought(question: str) -> str:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Think step by step before answering. Show your reasoning.",
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text


def strategy_simplified(question: str) -> str:
    simplify_prompt = (
        f"Rewrite this question in the simplest possible form, "
        f"then answer it: {question}"
    )
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": simplify_prompt}],
    )
    return r.content[0].text


def strategy_haiku_fallback(question: str) -> str:
    """Try a different model as a last resort."""
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text


STRATEGIES: list[tuple[str, Callable[[str], str]]] = [
    ("direct", strategy_direct),
    ("chain_of_thought", strategy_chain_of_thought),
    ("simplified", strategy_simplified),
    ("haiku_fallback", strategy_haiku_fallback),
]


def run_with_strategy_rotation(
    question: str,
    validator: Callable[[str], bool],
) -> str:
    for strategy_name, strategy_fn in STRATEGIES:
        try:
            output = strategy_fn(question)
            if validator(output):
                print(f"Strategy '{strategy_name}' succeeded")
                return output
            print(f"Strategy '{strategy_name}': output failed validation")
        except Exception as exc:
            print(f"Strategy '{strategy_name}' raised: {exc}")

    raise RuntimeError("All strategies failed")
```

**Expected Token Savings:** The first matching strategy short-circuits; later strategies are only tried if earlier ones fail, bounding worst-case cost.
**Environment:** High-stakes tasks where correctness matters more than cost; the fallback chain provides defense in depth.

---

### Option 5 — Structured retry state machine with backoff

```python
import anthropic
import asyncio
import random
from dataclasses import dataclass, field
from enum import Enum

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class RetryReason(Enum):
    VALIDATION_FAILED = "validation_failed"
    TOOL_ERROR = "tool_error"
    RATE_LIMIT = "rate_limit"
    EMPTY_RESPONSE = "empty_response"


@dataclass
class RetryState:
    attempt: int = 0
    reasons: list[RetryReason] = field(default_factory=list)
    last_output: str = ""
    last_error: str = ""

    def next_temperature(self) -> float:
        """Increase temperature with each attempt."""
        return min(0.0 + self.attempt * 0.25, 1.0)

    def next_max_tokens(self) -> int:
        """Give more tokens on retries in case truncation caused the failure."""
        return 512 + self.attempt * 256

    def build_retry_message(self, original_message: str) -> str:
        if not self.reasons:
            return original_message

        reason = self.reasons[-1]
        if reason == RetryReason.VALIDATION_FAILED:
            return (
                f"{original_message}\n\n"
                f"Previous attempt failed validation: {self.last_error}\n"
                f"Please correct the issue and try again."
            )
        if reason == RetryReason.EMPTY_RESPONSE:
            return f"{original_message}\n\nPlease provide a complete response."
        if reason == RetryReason.TOOL_ERROR:
            return (
                f"{original_message}\n\n"
                f"The tool returned an error: {self.last_error}\n"
                f"Try a different approach that doesn't use that tool."
            )
        return original_message


async def run_with_retry_state_machine(
    user_message: str,
    max_attempts: int = 4,
) -> str:
    state = RetryState()

    while state.attempt < max_attempts:
        prompt = state.build_retry_message(user_message)
        backoff = 0.0

        if state.attempt > 0 and RetryReason.RATE_LIMIT in state.reasons:
            backoff = random.uniform(1.0, 2.0 ** state.attempt)
            await asyncio.sleep(backoff)

        try:
            response = await async_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=state.next_max_tokens(),
                temperature=state.next_temperature(),
                messages=[{"role": "user", "content": prompt}],
            )
            output = response.content[0].text.strip()

            if not output:
                state.reasons.append(RetryReason.EMPTY_RESPONSE)
                state.last_error = "Empty response"
                state.attempt += 1
                continue

            # Validate here — example: require non-empty response
            state.last_output = output
            return output

        except anthropic.RateLimitError as exc:
            state.reasons.append(RetryReason.RATE_LIMIT)
            state.last_error = str(exc)
            state.attempt += 1

    raise RuntimeError(
        f"Exhausted {max_attempts} attempts. "
        f"Reasons: {[r.value for r in state.reasons]}"
    )
```

**Expected Token Savings:** State machine guarantees each retry is different; the variation axes (temperature, max_tokens, prompt, backoff) are tuned per failure reason.
**Environment:** Production agents needing structured observability into why retries happen; the `RetryState` object is also useful for logging and alerting.

---

### Option 6 — Semantic similarity check: abort if retry would be a duplicate

```python
import anthropic
import hashlib

client = anthropic.Anthropic(api_key="sk-live-...")


def _prompt_fingerprint(messages: list[dict], temperature: float) -> str:
    """Hash the prompt content + temperature to detect duplicate retries."""
    content = str(messages) + f"|temp={temperature:.2f}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class DeduplicatingRetrier:
    def __init__(self, max_attempts: int = 4) -> None:
        self.max_attempts = max_attempts
        self._seen_fingerprints: set[str] = set()

    def create(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        **kwargs,
    ) -> anthropic.types.Message:
        fp = _prompt_fingerprint(messages, temperature)

        if fp in self._seen_fingerprints:
            raise RuntimeError(
                f"Duplicate retry detected (fingerprint={fp}). "
                "Vary the prompt or temperature before retrying."
            )
        self._seen_fingerprints.add(fp)

        return client.messages.create(
            messages=messages,
            temperature=temperature,
            **kwargs,
        )


def run_agent(user_message: str) -> str:
    retrier = DeduplicatingRetrier(max_attempts=4)
    base_messages = [{"role": "user", "content": user_message}]

    for attempt in range(4):
        # Vary temperature to ensure each retry has a different fingerprint
        temp = attempt * 0.3

        try:
            response = retrier.create(
                messages=base_messages,
                temperature=temp,
                model="claude-sonnet-4-6",
                max_tokens=1024,
            )
            return response.content[0].text

        except RuntimeError as exc:
            if "Duplicate retry" in str(exc):
                raise   # programming error — stop immediately
            print(f"Attempt {attempt + 1} failed: {exc}")

    raise RuntimeError("All attempts failed")


# Comparison table
# | Option | Variation Axis | Extra Cost | Best For |
# |--------|---------------|------------|---------|
# | 1 Error injection | Prompt content | +1 turn | Validation failures |
# | 2 Temp escalation | Temperature | None | Deterministic format failures |
# | 3 Reformulation | Prompt structure | +50–100 tok | Persistent misunderstanding |
# | 4 Strategy rotation | Model + system | +1–2 calls | High-stakes correctness |
# | 5 State machine | Multiple axes | Structured | Production observability |
# | 6 Dedup guard | Fingerprint check | None | Prevent programming errors |
```

**Expected Token Savings:** The fingerprint guard makes duplicate retries impossible at the call level — it forces the caller to introduce variation or crash loudly, preventing silent token waste.
**Environment:** Development and CI environments where retry bugs are caught before production; the guard surfaces the bug immediately rather than silently burning tokens.
