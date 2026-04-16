---
layout: solution
title: "Agent Doesn't Implement Graceful Error Message Generation for End Users"
category: general
description: "Transform raw internal errors, API failures, and technical exceptions into clear, actionable, user-friendly messages that explain what went wrong and what the user can do next."
tags: [general, error-handling, ux, user-experience, error-messages, resilience, communication]
---

# Agent Doesn't Implement Graceful Error Message Generation for End Users

## Problem

When an agent fails — due to rate limits, API errors, tool failures, context overflow, or invalid input — it either exposes raw technical error messages (`anthropic.RateLimitError: 429 Too Many Requests`) or silently returns nothing. Users receive confusing stack traces, cryptic status codes, or empty responses. Without graceful error message generation, technical failures become user-facing failures that erode trust and provide no path forward.

## Solutions

### Option 1: Error Classifier with User-Friendly Templates

Classify exceptions into user-facing categories and render templated messages for each category.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class UserError:
    title: str
    message: str
    suggestion: str
    retry_allowed: bool = True
    error_code: str = ""

    def format(self) -> str:
        lines = [f"**{self.title}**", self.message]
        if self.suggestion:
            lines.append(f"What you can do: {self.suggestion}")
        if self.error_code:
            lines.append(f"(Error code: {self.error_code})")
        return "\n".join(lines)


ERROR_TEMPLATES: dict[str, UserError] = {
    "rate_limit": UserError(
        title="Too Many Requests",
        message="The assistant is currently busy handling many requests.",
        suggestion="Please wait a moment and try again.",
        retry_allowed=True,
        error_code="RATE_LIMIT",
    ),
    "overload": UserError(
        title="Service Temporarily Unavailable",
        message="The AI service is experiencing high demand right now.",
        suggestion="Try again in 30–60 seconds.",
        retry_allowed=True,
        error_code="OVERLOAD",
    ),
    "context_overflow": UserError(
        title="Conversation Too Long",
        message="This conversation has grown too long for the assistant to process.",
        suggestion="Start a new conversation or ask a shorter, more focused question.",
        retry_allowed=False,
        error_code="CONTEXT_OVERFLOW",
    ),
    "invalid_request": UserError(
        title="Request Could Not Be Processed",
        message="Something about your request couldn't be understood.",
        suggestion="Try rephrasing your question or breaking it into smaller parts.",
        retry_allowed=True,
        error_code="INVALID_REQUEST",
    ),
    "auth_error": UserError(
        title="Authentication Error",
        message="There's a configuration issue with the assistant service.",
        suggestion="Please contact support or check that your account is active.",
        retry_allowed=False,
        error_code="AUTH_ERROR",
    ),
    "tool_failure": UserError(
        title="A Tool Encountered an Error",
        message="One of the tools the assistant tried to use didn't work as expected.",
        suggestion="Try asking your question in a different way, or try again.",
        retry_allowed=True,
        error_code="TOOL_FAILURE",
    ),
    "unknown": UserError(
        title="Something Went Wrong",
        message="An unexpected error occurred.",
        suggestion="Please try again. If the problem persists, contact support.",
        retry_allowed=True,
        error_code="UNKNOWN_ERROR",
    ),
}


def classify_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 529:
            return "overload"
        if exc.status_code in (400, 422):
            return "invalid_request"
        if exc.status_code in (401, 403):
            return "auth_error"
    if "context" in str(exc).lower() or "too long" in str(exc).lower():
        return "context_overflow"
    return "unknown"


def safe_chat(user_message: str) -> str:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text
    except anthropic.AnthropicError as e:
        category = classify_error(e)
        user_error = ERROR_TEMPLATES[category]
        print(f"  [Internal: {type(e).__name__}: {e}]")
        return user_error.format()
    except Exception as e:
        user_error = ERROR_TEMPLATES["unknown"]
        print(f"  [Internal: {type(e).__name__}: {e}]")
        return user_error.format()


if __name__ == "__main__":
    questions = [
        "What is machine learning?",
        "Tell me about Python.",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        answer = safe_chat(q)
        print(f"A: {answer[:200]}")

# Expected Token Savings: No extra tokens; graceful messages prevent frustrated re-tries
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: LLM-Generated Contextual Error Messages

Use a small LLM call to generate a contextual, empathetic error message based on what the user was trying to do.

```python
import anthropic
import time

client = anthropic.Anthropic()


def generate_user_message(
    error_type: str,
    user_intent: str,
    technical_detail: str = "",
) -> str:
    """Use the LLM to craft an empathetic, contextual error message."""
    prompt = (
        f"A user was trying to: {user_intent}\n"
        f"An error occurred: {error_type}\n"
        f"Technical detail (do NOT share with user): {technical_detail[:100]}\n\n"
        "Write a brief, friendly, non-technical message explaining what happened "
        "and what the user can do next. Be empathetic, clear, and concise (2–3 sentences max). "
        "Do not use technical jargon or mention error codes."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception:
        return "Something went wrong. Please try again in a moment."


def extract_user_intent(message: str) -> str:
    """Extract a brief description of what the user wanted."""
    words = message.strip().split()[:15]
    return " ".join(words) + ("..." if len(message.split()) > 15 else "")


def safe_chat_with_context(
    conversation: list[dict],
    user_message: str,
    max_retries: int = 1,
) -> str:
    user_intent = extract_user_intent(user_message)
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            messages = conversation + [{"role": "user", "content": user_message}]
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=messages,
            )
            return resp.content[0].text

        except anthropic.RateLimitError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return generate_user_message(
                "service temporarily busy",
                user_intent,
                str(e)[:80],
            )

        except anthropic.APIStatusError as e:
            last_exc = e
            if e.status_code >= 500:
                return generate_user_message(
                    "temporary service disruption",
                    user_intent,
                    f"HTTP {e.status_code}",
                )
            return generate_user_message(
                "request could not be completed",
                user_intent,
                str(e)[:80],
            )

        except Exception as e:
            last_exc = e
            return generate_user_message(
                "unexpected error",
                user_intent,
                str(e)[:80],
            )

    return generate_user_message("repeated failure", user_intent, str(last_exc)[:80])


if __name__ == "__main__":
    conv: list[dict] = []
    questions = [
        "Explain the concept of machine learning to me.",
        "How can I get started with Python programming?",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        reply = safe_chat_with_context(conv, q)
        conv.append({"role": "user", "content": q})
        conv.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:200]}")

# Expected Token Savings: One small haiku call per error vs. many frustrated user retries
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Error Recovery with Partial Results

When an error occurs mid-task, return whatever partial results were accumulated along with a graceful explanation.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class PartialResult:
    completed_steps: list[dict] = field(default_factory=list)
    failed_at: str = ""
    error_message: str = ""
    is_complete: bool = False

    def add_step(self, name: str, result: str) -> None:
        self.completed_steps.append({"step": name, "result": result})

    def format_for_user(self) -> str:
        if self.is_complete:
            parts = [r["result"] for r in self.completed_steps]
            return "\n\n".join(parts)

        # partial result with graceful error
        lines = []
        if self.completed_steps:
            lines.append("Here's what I was able to find before encountering an issue:\n")
            for step in self.completed_steps:
                lines.append(f"**{step['step'].replace('_', ' ').title()}**")
                lines.append(step["result"])
                lines.append("")

        if self.error_message:
            lines.append(f"---\n{self.error_message}")

        return "\n".join(lines) if lines else self.error_message


def run_multi_step_task(topic: str) -> str:
    result = PartialResult()
    steps = [
        ("overview",      f"Give a 2-sentence overview of: {topic}"),
        ("key_points",    f"List 3 key points about: {topic}"),
        ("applications",  f"Name 2 real-world applications of: {topic}"),
        ("summary",       f"Write a concluding sentence about: {topic}"),
    ]

    for step_name, prompt in steps:
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            result.add_step(step_name, resp.content[0].text)
        except anthropic.RateLimitError:
            result.failed_at = step_name
            result.error_message = (
                "I was able to gather some information, but the service became temporarily unavailable. "
                "The partial results above are what I found. Please try again in a moment for the complete answer."
            )
            break
        except anthropic.APIStatusError as e:
            result.failed_at = step_name
            result.error_message = (
                f"I encountered an issue while researching '{step_name.replace('_', ' ')}'. "
                "The information above is what I gathered before the problem occurred. "
                "You may want to ask specifically about the remaining aspects."
            )
            break
        except Exception:
            result.failed_at = step_name
            result.error_message = (
                "Something unexpected happened partway through. "
                "I've included what I found so far — please try again for the rest."
            )
            break
    else:
        result.is_complete = True

    return result.format_for_user()


if __name__ == "__main__":
    topics = ["quantum computing", "transformer neural networks"]
    for topic in topics:
        print(f"\n{'='*50}\nTopic: {topic}\n{'='*50}")
        answer = run_multi_step_task(topic)
        print(answer[:500])

# Expected Token Savings: Partial results avoid full re-runs when only later steps fail
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Error Message Localizer with Severity Levels

Generate error messages in the user's language and tone, calibrated to the severity of the error.

```python
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()


class ErrorSeverity(Enum):
    TRANSIENT  = "transient"    # will likely resolve on retry
    DEGRADED   = "degraded"     # service works but slower/limited
    BLOCKED    = "blocked"      # user needs to take action
    SYSTEM     = "system"       # operator action needed


@dataclass
class LocalizedError:
    severity: ErrorSeverity
    title: str
    body: str
    cta: str          # call to action
    show_retry: bool

    def to_response(self) -> str:
        icon = {
            ErrorSeverity.TRANSIENT: "⏳",
            ErrorSeverity.DEGRADED:  "⚠️",
            ErrorSeverity.BLOCKED:   "🚫",
            ErrorSeverity.SYSTEM:    "🔧",
        }.get(self.severity, "❌")
        parts = [f"{icon} **{self.title}**", self.body]
        if self.cta:
            parts.append(f"\n→ {self.cta}")
        if self.show_retry:
            parts.append("_(You can try again shortly.)_")
        return "\n".join(parts)


ERROR_LIBRARY: dict[str, LocalizedError] = {
    "rate_limit": LocalizedError(
        severity=ErrorSeverity.TRANSIENT,
        title="Busy Right Now",
        body="The assistant is handling a lot of requests at the moment.",
        cta="Wait 10–15 seconds and try again.",
        show_retry=True,
    ),
    "overloaded": LocalizedError(
        severity=ErrorSeverity.DEGRADED,
        title="High Demand",
        body="Response times are slower than usual due to high traffic.",
        cta="Your request may take longer. Please be patient.",
        show_retry=True,
    ),
    "context_too_long": LocalizedError(
        severity=ErrorSeverity.BLOCKED,
        title="Conversation Too Long",
        body="This conversation has reached its memory limit.",
        cta="Start a new conversation and summarize what you need from this one.",
        show_retry=False,
    ),
    "content_filtered": LocalizedError(
        severity=ErrorSeverity.BLOCKED,
        title="Request Not Allowed",
        body="Your request couldn't be completed due to content guidelines.",
        cta="Try rephrasing your question.",
        show_retry=True,
    ),
    "tool_timeout": LocalizedError(
        severity=ErrorSeverity.TRANSIENT,
        title="Tool Timed Out",
        body="A tool the assistant was using took too long to respond.",
        cta="Try again — it usually works on the second attempt.",
        show_retry=True,
    ),
    "auth_expired": LocalizedError(
        severity=ErrorSeverity.BLOCKED,
        title="Session Expired",
        body="Your session has expired.",
        cta="Please log in again to continue.",
        show_retry=False,
    ),
    "unknown": LocalizedError(
        severity=ErrorSeverity.SYSTEM,
        title="Unexpected Error",
        body="Something unexpected happened on our end.",
        cta="Try again. If this keeps happening, contact support.",
        show_retry=True,
    ),
}


def classify_exception(exc: Exception, context: dict | None = None) -> str:
    context = context or {}
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        if code == 529:
            return "overloaded"
        if code in (401, 403):
            return "auth_expired"
        if code == 400:
            body = str(exc).lower()
            if "content" in body or "policy" in body:
                return "content_filtered"
            if "context" in body or "too long" in body:
                return "context_too_long"
    if context.get("from_tool"):
        return "tool_timeout"
    return "unknown"


def safe_respond(user_message: str, context: dict | None = None) -> str:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text
    except Exception as exc:
        error_key = classify_exception(exc, context)
        err = ERROR_LIBRARY.get(error_key, ERROR_LIBRARY["unknown"])
        print(f"  [Internal error: {type(exc).__name__}]")
        return err.to_response()


if __name__ == "__main__":
    messages = [
        "Explain transformer architecture.",
        "What is the attention mechanism?",
    ]
    for msg in messages:
        print(f"\nQ: {msg}")
        reply = safe_respond(msg)
        print(f"A: {reply[:300]}")

# Expected Token Savings: Avoids confused user follow-ups by giving clear next steps
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Error Boundary with Fallback Strategies

Implement layered fallback strategies: retry → degraded mode → cached response → graceful decline.

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

RESPONSE_CACHE: dict[str, str] = {}


@dataclass
class FallbackChain:
    max_retries: int = 2
    retry_delay: float = 1.0
    enable_cache: bool = True
    enable_degraded: bool = True

    def _try_primary(self, message: str, max_tokens: int) -> str:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        result = resp.content[0].text
        if self.enable_cache:
            RESPONSE_CACHE[message[:100]] = result
        return result

    def _try_degraded(self, message: str) -> str:
        """Fall back to a smaller, faster model with reduced max_tokens."""
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Give a brief answer to: {message}"}],
        )
        return f"[Brief answer — full response unavailable]\n{resp.content[0].text}"

    def _try_cache(self, message: str) -> str | None:
        key = message[:100]
        return RESPONSE_CACHE.get(key)

    def _graceful_decline(self, reason: str) -> str:
        messages_by_reason = {
            "rate_limit":   "The assistant is temporarily busy. Please try again in a few seconds.",
            "overload":     "The service is under high load. Your request has been noted — please retry shortly.",
            "auth":         "There's an account issue preventing responses. Please check your settings.",
            "context":      "This conversation is too long to process. Start a new one with a focused question.",
            "repeated_fail":"Despite several attempts, the assistant couldn't complete this request. Please try again later.",
        }
        return messages_by_reason.get(reason, "Something went wrong. Please try again.")

    def execute(self, message: str, max_tokens: int = 1024) -> tuple[str, str]:
        """Returns (response, strategy_used)."""
        # 1. Try primary with retries
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._try_primary(message, max_tokens), "primary"
            except anthropic.RateLimitError as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * (2 ** attempt))
            except anthropic.APIStatusError as e:
                last_exc = e
                if e.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay)
                    continue
                break   # non-retriable
            except Exception as e:
                last_exc = e
                break

        # 2. Try cache
        if self.enable_cache:
            cached = self._try_cache(message)
            if cached:
                return f"[Cached response]\n{cached}", "cache"

        # 3. Try degraded mode
        if self.enable_degraded:
            try:
                return self._try_degraded(message), "degraded"
            except Exception:
                pass

        # 4. Graceful decline
        reason = "rate_limit" if isinstance(last_exc, anthropic.RateLimitError) else "repeated_fail"
        return self._graceful_decline(reason), "declined"


fallback = FallbackChain()


def chat(message: str) -> None:
    response, strategy = fallback.execute(message)
    print(f"  Strategy: {strategy}")
    print(f"  Response: {response[:150]}\n")


if __name__ == "__main__":
    queries = [
        "What is the capital of France?",
        "Explain neural networks briefly.",
        "What is asyncio in Python?",
    ]
    for q in queries:
        print(f"Q: {q}")
        chat(q)

# Expected Token Savings: Degraded mode uses haiku at 1/5 cost; cache avoids repeat billing
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Async Error Boundary with User Notification and Telemetry

Full production error boundary: async execution, user notification, internal telemetry, and structured incident reporting.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

client = anthropic.AsyncAnthropic()


@dataclass
class ErrorIncident:
    incident_id: str = field(default_factory=lambda: uuid4().hex[:8])
    error_type: str = ""
    user_message: str = ""
    user_visible_message: str = ""
    technical_detail: str = ""
    strategy: str = ""
    ts: float = field(default_factory=time.time)
    resolved: bool = False


INCIDENT_LOG: list[ErrorIncident] = []


def user_message_for(exc: Exception, incident_id: str) -> str:
    if isinstance(exc, anthropic.RateLimitError):
        return (
            "The assistant is temporarily at capacity. "
            "Please try again in a few seconds. "
            f"(Ref: {incident_id})"
        )
    if isinstance(exc, asyncio.TimeoutError):
        return (
            "Your request took too long to process. "
            "This is usually temporary — please try again. "
            f"(Ref: {incident_id})"
        )
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return (
            "The AI service is experiencing difficulties. "
            "We've logged this issue automatically. "
            f"Please try again shortly. (Ref: {incident_id})"
        )
    return (
        "Something unexpected happened. "
        "Please try again. If the issue continues, contact support "
        f"with reference: {incident_id}"
    )


async def notify_user(user_id: str, message: str) -> None:
    """Simulate sending a notification (webhook, email, in-app)."""
    print(f"  [NOTIFY → {user_id}]: {message[:80]}")


async def log_telemetry(incident: ErrorIncident) -> None:
    """Simulate sending to a telemetry/alerting system."""
    print(f"  [TELEMETRY] incident={incident.incident_id} type={incident.error_type} resolved={incident.resolved}")
    INCIDENT_LOG.append(incident)


async def safe_async_chat(
    user_id: str,
    user_message: str,
    timeout: float = 15.0,
) -> str:
    incident = ErrorIncident(
        user_message=user_message[:100],
    )
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": user_message}],
            ),
            timeout=timeout,
        )
        incident.resolved = True
        incident.strategy = "primary"
        asyncio.create_task(log_telemetry(incident))
        return resp.content[0].text

    except asyncio.TimeoutError as exc:
        incident.error_type = "timeout"
        incident.technical_detail = f"Timeout after {timeout}s"

    except anthropic.RateLimitError as exc:
        incident.error_type = "rate_limit"
        incident.technical_detail = str(exc)[:100]

    except anthropic.APIStatusError as exc:
        incident.error_type = f"api_status_{exc.status_code}"
        incident.technical_detail = str(exc)[:100]

    except Exception as exc:
        incident.error_type = "unknown"
        incident.technical_detail = str(exc)[:100]

    # build user-visible message
    user_msg = user_message_for(
        Exception(incident.error_type),
        incident.incident_id,
    )
    incident.user_visible_message = user_msg
    incident.strategy = "graceful_decline"

    # fire-and-forget: notify user and log telemetry
    await asyncio.gather(
        notify_user(user_id, user_msg),
        log_telemetry(incident),
    )
    return user_msg


async def main() -> None:
    user_id = "user_123"
    prompts = [
        "What is machine learning?",
        "Explain gradient descent simply.",
        "What is a neural network?",
    ]
    for p in prompts:
        print(f"\nQ: {p}")
        reply = await safe_async_chat(user_id, p)
        print(f"A: {reply[:150]}")

    print(f"\nIncident log: {len(INCIDENT_LOG)} events, {sum(1 for i in INCIDENT_LOG if i.resolved)} resolved")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Structured error handling prevents panic retries; telemetry enables fast fixes
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | User-Friendliness | Contextual | Partial Results | Fallback | Telemetry | Best For |
|--------|------------------|-----------|----------------|---------|-----------|----------|
| 1 | High | Low | No | No | No | Simple apps with known error types |
| 2 | Very High | Yes | No | No | No | Apps where context matters |
| 3 | High | Medium | Yes | No | No | Multi-step pipelines |
| 4 | High | Medium | No | No | No | Multi-language or tone-sensitive products |
| 5 | High | Low | No | Yes | No | Production apps with graceful degradation |
| 6 | Very High | High | No | No | Yes | Production with alerting and SLOs |
