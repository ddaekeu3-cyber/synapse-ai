---
layout: solution
title: "Agent gets stuck when model declines request"
category: loop-stuck
description: "When the model refuses or declines to fulfill a request (due to content policy, ambiguity, or over-caution), the agent has no fallback path. It retries the same refused request, or worse, enters an infinite clarification loop asking the user the same question repeatedly."
tags: [loop-stuck, refusal, content-policy, fallback, error-handling, prompt-engineering]
---

## Symptom

The agent calls the model and receives a refusal response: "I can't help with that", "I'm not sure what you're asking", or "As an AI, I don't..." — but instead of handling the refusal gracefully, the agent retries the same request unchanged, loops asking the user for clarification it already received, or crashes with an unhandled response type.

## Root Cause

The agent only handles two response states: `stop_reason == "end_turn"` (success) and `stop_reason == "tool_use"` (use a tool). A refusal is technically an `end_turn` response, but the content signals that the model did not fulfill the task. Without detecting and routing the refusal, the agent cannot distinguish "task complete" from "task declined".

## Fix

Detect refusal responses by checking the content for refusal signals. Route detected refusals to a fallback handler: rephrase the request, escalate to a human, return a graceful error, or try a different strategy.

---

### Option 1 — Refusal detection with keyword scan

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

REFUSAL_SIGNALS = [
    "i can't help with",
    "i cannot help with",
    "i'm not able to",
    "i am not able to",
    "i won't be able to",
    "as an ai",
    "i don't have the ability",
    "i'm unable to",
    "i'm sorry, but i can't",
    "i'm not going to",
    "that's not something i can",
    "i'm not comfortable",
    "i'd prefer not to",
    "i need to decline",
]

AMBIGUITY_SIGNALS = [
    "could you clarify",
    "what do you mean by",
    "could you provide more detail",
    "i'm not sure what you're asking",
    "can you be more specific",
]


def classify_response(text: str) -> str:
    """Returns: 'success' | 'refusal' | 'ambiguous'"""
    lower = text.lower()
    if any(s in lower for s in REFUSAL_SIGNALS):
        return "refusal"
    if any(s in lower for s in AMBIGUITY_SIGNALS):
        return "ambiguous"
    return "success"


def run_agent(user_message: str, max_rephrases: int = 2) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    rephrases = 0

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text
        status = classify_response(output)

        if status == "success":
            return output

        if status == "refusal":
            if rephrases >= max_rephrases:
                return (
                    "I wasn't able to complete this task as requested. "
                    "Please rephrase your request or contact support."
                )
            # Rephrase: ask the model to try a different approach
            messages.append({"role": "assistant", "content": output})
            messages.append({
                "role": "user",
                "content": (
                    "Could you approach this differently? "
                    "If there's a concern, please tell me what information you need "
                    "to fulfill the request."
                ),
            })
            rephrases += 1

        elif status == "ambiguous":
            # Only ask for clarification once
            if any(m["role"] == "user" and "clarify" in m["content"].lower() for m in messages):
                return output  # already asked, accept whatever came back
            messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": user_message + " Please proceed with your best interpretation."})
```

**Expected Token Savings:** Bounded retry count prevents infinite refusal loops; the fallback message is returned instead of burning tokens on guaranteed-to-fail retries.
**Environment:** General-purpose agents; keyword detection is fast and zero-cost.

---

### Option 2 — Refusal classifier with Haiku as a routing judge

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CLASSIFIER_SYSTEM = (
    "Classify this AI assistant response as exactly one of:\n"
    "  fulfilled — the assistant completed the user's request\n"
    "  refused — the assistant declined or said it can't help\n"
    "  clarifying — the assistant asked for more information\n"
    "  partial — the assistant partially addressed the request\n"
    "Reply with exactly one word."
)


def classify_with_llm(response_text: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": response_text[:1000]}],
    )
    return r.content[0].text.strip().lower()


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    rephrase_count = 0

    for _ in range(4):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text
        status = classify_with_llm(output)
        print(f"Response classified as: {status}")

        if status == "fulfilled":
            return output

        if status == "partial":
            return output  # accept partial as best available

        if status == "refused" and rephrase_count < 2:
            messages.append({"role": "assistant", "content": output})
            messages.append({
                "role": "user",
                "content": "Please help with this using a different approach or tell me what you need.",
            })
            rephrase_count += 1
            continue

        if status == "clarifying":
            # Don't loop on clarification — accept and proceed
            return output

        # Exhausted rephrases or unknown status
        return (
            "I was unable to complete your request. "
            f"Last response: {output[:200]}"
        )

    return ""
```

**Expected Token Savings:** Haiku classifier costs ~20 tokens; prevents 2–4 unnecessary retry turns (each ~500 tokens) on a refused request.
**Environment:** Agents where keyword-based detection is insufficient; the LLM judge handles nuanced refusals and partial responses correctly.

---

### Option 3 — Fallback chain: try alternative approaches on refusal

```python
import anthropic
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")

REFUSAL_KEYWORDS = ["can't help", "cannot help", "unable to", "won't", "as an ai"]


def is_refusal(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in REFUSAL_KEYWORDS)


def attempt_direct(user_message: str, system: str) -> str:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


def attempt_with_context(user_message: str, system: str) -> str:
    """Add explicit professional context to reduce over-caution."""
    augmented = (
        f"Context: This is for a legitimate professional use case in a business setting. "
        f"Request: {user_message}"
    )
    return attempt_direct(augmented, system)


def attempt_decomposed(user_message: str, system: str) -> str:
    """Ask the model to decompose the task and address each part separately."""
    decompose_msg = (
        f"Please break this request into individual parts and answer each: {user_message}"
    )
    return attempt_direct(decompose_msg, system)


def attempt_haiku_fallback(user_message: str, _system: str) -> str:
    """Try a different model."""
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


FALLBACK_CHAIN: list[Callable[[str, str], str]] = [
    attempt_direct,
    attempt_with_context,
    attempt_decomposed,
    attempt_haiku_fallback,
]


def run_agent(user_message: str, system: str = "You are a helpful assistant.") -> str:
    for i, strategy in enumerate(FALLBACK_CHAIN):
        output = strategy(user_message, system)
        if not is_refusal(output):
            if i > 0:
                print(f"Succeeded with fallback strategy {i + 1}")
            return output
        print(f"Strategy {i + 1} resulted in refusal")

    return "Unable to process this request with any available strategy."
```

**Expected Token Savings:** Each fallback costs one model call; the chain stops at the first success, so most requests resolve on attempt 1 with no extra cost.
**Environment:** Agents where content policy refusals are rare but need graceful handling; the chain provides defense in depth without complex logic.

---

### Option 4 — System prompt hardening to preempt unnecessary refusals

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

ANTI_REFUSAL_SYSTEM = """\
You are a helpful assistant for a professional business application.

Guidelines for handling requests:
- Always make a good-faith attempt to help with any business-related request
- If a request is ambiguous, choose the most reasonable professional interpretation and proceed
- Do not add unsolicited disclaimers, caveats, or refusals for routine business tasks
- If you genuinely cannot fulfill a request, explain specifically WHY and WHAT you need
- Never respond with "As an AI, I..." — just address the request
- If something is outside your capabilities, say "I don't have access to X" rather than "I won't"
- For sensitive topics, provide professional, factual information appropriate to a business context

Your goal is to be maximally helpful while remaining accurate and professional.
"""


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ANTI_REFUSAL_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Adds ~120 tokens to system prompt; prevents refusal responses that would require 1–3 extra correction turns (~500–1500 tokens).
**Environment:** Business applications where the request space is well-defined and the default model over-caution causes unnecessary friction; tune the system prompt to your domain.

---

### Option 5 — Structured refusal handler with user-facing recovery

```python
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic(api_key="sk-live-...")


class ResponseType(Enum):
    SUCCESS = "success"
    REFUSAL_POLICY = "refusal_policy"       # won't do it
    REFUSAL_CAPABILITY = "refusal_capability"  # can't do it
    NEEDS_CLARIFICATION = "needs_clarification"
    PARTIAL = "partial"


SIGNAL_MAP: dict[ResponseType, list[str]] = {
    ResponseType.REFUSAL_POLICY: [
        "i won't", "i will not", "i'm not going to", "i'd rather not",
        "i'm not comfortable", "i don't think i should",
    ],
    ResponseType.REFUSAL_CAPABILITY: [
        "i can't", "i cannot", "i'm unable to", "i don't have access",
        "i don't have the ability", "i'm not able to",
    ],
    ResponseType.NEEDS_CLARIFICATION: [
        "could you clarify", "can you elaborate", "what do you mean",
        "could you provide more", "i'm not sure what",
    ],
}


def classify_response_type(text: str) -> ResponseType:
    lower = text.lower()
    for rtype, signals in SIGNAL_MAP.items():
        if any(s in lower for s in signals):
            return rtype
    return ResponseType.SUCCESS


@dataclass
class AgentResult:
    success: bool
    content: str
    response_type: ResponseType
    recovery_suggestion: str = ""


def run_agent(user_message: str) -> AgentResult:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text
    rtype = classify_response_type(output)

    recovery_suggestions = {
        ResponseType.REFUSAL_POLICY: (
            "This request may need rephrasing or additional context. "
            "Try adding professional context or breaking it into smaller steps."
        ),
        ResponseType.REFUSAL_CAPABILITY: (
            "The agent lacks the capability for this request. "
            "Consider using a tool or providing the required data directly."
        ),
        ResponseType.NEEDS_CLARIFICATION: (
            "The agent needs more information. Please answer its question and retry."
        ),
    }

    return AgentResult(
        success=(rtype in (ResponseType.SUCCESS, ResponseType.PARTIAL)),
        content=output,
        response_type=rtype,
        recovery_suggestion=recovery_suggestions.get(rtype, ""),
    )


result = run_agent("Summarize our Q3 financials.")
if not result.success:
    print(f"Response type: {result.response_type.value}")
    print(f"Recovery: {result.recovery_suggestion}")
```

**Expected Token Savings:** None — adds structure on top of the detection; the `AgentResult` makes refusal handling explicit in the calling code rather than silently retrying.
**Environment:** Production agents where callers need to distinguish "task done" from "task declined" for downstream routing (notify human, retry differently, log for review).

---

### Option 6 — Async refusal watchdog: detects and escalates without blocking

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

REFUSAL_PATTERNS = [
    "i can't", "i cannot", "i won't", "i'm unable",
    "as an ai", "i don't have the ability",
]

ESCALATION_QUEUE: asyncio.Queue[dict] = asyncio.Queue()


def is_refusal(text: str) -> bool:
    return any(p in text.lower() for p in REFUSAL_PATTERNS)


async def escalation_worker() -> None:
    """Background worker that processes escalated refusals."""
    while True:
        item = await ESCALATION_QUEUE.get()
        print(f"[ESCALATION] user_id={item['user_id']} message={item['message'][:80]}")
        # In production: send to Slack, create a support ticket, log to a DB
        ESCALATION_QUEUE.task_done()


async def run_agent_async(
    user_id: str,
    user_message: str,
    max_retries: int = 2,
) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    retry = 0

    while retry <= max_retries:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text

        if not is_refusal(output):
            return output

        retry += 1
        if retry <= max_retries:
            messages.append({"role": "assistant", "content": output})
            messages.append({
                "role": "user",
                "content": "Please try to help with this using an alternative approach.",
            })
        else:
            # Escalate without blocking the response
            await ESCALATION_QUEUE.put({
                "user_id": user_id,
                "message": user_message,
                "final_response": output,
            })
            return (
                "I wasn't able to help with this request directly. "
                "I've flagged it for follow-up. "
                f"In the meantime: {output[:200]}"
            )

    return ""


async def main() -> None:
    worker = asyncio.create_task(escalation_worker())
    result = await run_agent_async("user_42", "Help me with a complex task.")
    print(result)
    await ESCALATION_QUEUE.join()
    worker.cancel()


# Comparison table
# | Option | Detection | Fallback Strategy | Escalation |
# |--------|-----------|------------------|------------|
# | 1 Keyword scan | Regex patterns | Rephrase prompt | No |
# | 2 Haiku judge | LLM classification | Rephrase | No |
# | 3 Fallback chain | Keyword | Multiple strategies | No |
# | 4 System hardening | None (prevention) | N/A | No |
# | 5 Structured result | Keyword | Caller decides | No |
# | 6 Async watchdog | Keyword | Rephrase + queue | Yes |

asyncio.run(main())
```

**Expected Token Savings:** Max retry count prevents infinite refusal loops; escalation queue ensures declined requests are reviewed without adding latency to the user response.
**Environment:** Production async agents where some refusals need human review; the queue decouples escalation from the response path.
