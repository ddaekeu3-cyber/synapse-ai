---
title: "Agent Doesn't Implement Fallback Response Templates When LLM Is Unavailable"
description: "Agents that propagate raw LLM provider errors to users during outages expose internal error messages and degrade the user experience abruptly. When the LLM is unavailable, the agent should return a graceful fallback response that acknowledges the disruption, preserves context for retry, and optionally routes simple requests to a template-based response path. Implement fallback response templates that activate on LLM availability failures and provide meaningful responses without model inference."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-fallback-response-templates-when-llm-is-unavailable
tags: [fallback-response, llm-unavailability, graceful-degradation, response-templates, outage-handling, user-experience]
symptoms:
  - "Users see raw 503 or provider error messages when the LLM API is down"
  - "Agent returns empty responses or Python tracebacks during provider outages"
  - "No user-facing acknowledgment that the service is temporarily unavailable"
  - "Simple factual requests that could be answered without inference get no response"
  - "Retry context is lost because the session is abandoned on first LLM failure"
---

## Why This Happens

LLM availability is treated as a prerequisite rather than a variable. When the model API returns a 503 or timeout, the exception propagates up the call stack and is either swallowed silently or surfaced as a raw error. Fallback handling requires an explicit layer in the response path that intercepts LLM failures, classifies the request type, and selects an appropriate template. For requests that cannot be answered without inference, the fallback informs the user and preserves their input for retry. For simple intents (greetings, status checks, help requests), canned responses require no model at all.

## Solution 1: LLM Availability Signal

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LLMAvailabilityState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"       # slow but responding
    UNAVAILABLE = "unavailable" # returning errors
    UNKNOWN = "unknown"         # not yet observed


@dataclass
class LLMAvailabilitySignal:
    state: LLMAvailabilityState
    error_type: Optional[str] = None
    provider: str = ""
    observed_at: float = field(default_factory=time.time)
    consecutive_failures: int = 0

    def is_fallback_required(self) -> bool:
        return self.state == LLMAvailabilityState.UNAVAILABLE
```

## Solution 2: Intent Classifier for Fallback Routing

```python
import re
from enum import Enum
from typing import Optional


class FallbackIntent(str, Enum):
    GREETING = "greeting"
    HELP_REQUEST = "help_request"
    STATUS_CHECK = "status_check"
    CANCELLATION = "cancellation"
    GRATITUDE = "gratitude"
    UNKNOWN = "unknown"           # requires inference — serve unavailability message


_INTENT_PATTERNS = [
    (FallbackIntent.GREETING, re.compile(
        r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|greetings)\b", re.IGNORECASE
    )),
    (FallbackIntent.HELP_REQUEST, re.compile(
        r"\b(help|how (do|can) (i|you)|what can you do|commands|features)\b", re.IGNORECASE
    )),
    (FallbackIntent.STATUS_CHECK, re.compile(
        r"\b(status|are you (working|up|available)|is (this|the service) (working|up))\b",
        re.IGNORECASE,
    )),
    (FallbackIntent.CANCELLATION, re.compile(
        r"^\s*(cancel|stop|nevermind|never mind|abort|quit|exit)\b", re.IGNORECASE
    )),
    (FallbackIntent.GRATITUDE, re.compile(
        r"^\s*(thanks|thank you|thx|ty|cheers|appreciated)\b", re.IGNORECASE
    )),
]


class FallbackIntentClassifier:
    """
    Classifies user input into a fallback-serviceable intent
    using lightweight regex patterns. Returns UNKNOWN for complex
    requests that require LLM inference.
    """

    def classify(self, user_input: str) -> FallbackIntent:
        for intent, pattern in _INTENT_PATTERNS:
            if pattern.search(user_input):
                return intent
        return FallbackIntent.UNKNOWN
```

## Solution 3: Fallback Response Template Registry

```python
from typing import Callable, Dict, Optional


_DEFAULT_TEMPLATES: Dict[str, str] = {
    "greeting": (
        "Hello! I'm temporarily operating in limited mode. "
        "I can answer basic questions about my capabilities, but detailed assistance "
        "will be available again shortly."
    ),
    "help_request": (
        "I can assist with a wide range of tasks including answering questions, "
        "summarizing content, writing, and analysis. I'm currently experiencing "
        "a brief service interruption — please try your request again in a moment."
    ),
    "status_check": (
        "I'm currently experiencing limited availability due to a service disruption. "
        "My team is aware and working on a resolution. Please try again shortly."
    ),
    "cancellation": (
        "Understood. Your request has been cancelled. Let me know when you'd like to try again."
    ),
    "gratitude": (
        "You're welcome! Feel free to reach out whenever you're ready."
    ),
    "unavailable_generic": (
        "I'm sorry, I'm temporarily unable to process your request due to a service disruption. "
        "Your message has been noted. Please try again in a few minutes."
    ),
    "unavailable_with_retry": (
        "I'm experiencing a brief outage and couldn't process your request right now. "
        "I've saved your message: \"{user_input}\"\n\n"
        "Please try again shortly and I'll pick up where we left off."
    ),
}


class FallbackResponseTemplateRegistry:
    """
    Stores and renders fallback response templates.
    Supports variable substitution via format kwargs.
    """

    def __init__(self):
        self._templates: Dict[str, str] = dict(_DEFAULT_TEMPLATES)

    def register(self, key: str, template: str) -> None:
        self._templates[key] = template

    def render(self, key: str, **kwargs) -> Optional[str]:
        template = self._templates.get(key)
        if template is None:
            return None
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
```

## Solution 4: Fallback Response Selector

```python
from typing import Optional


class FallbackResponseSelector:
    """
    Given an availability signal and user input, selects and renders
    the appropriate fallback response. Returns None if the LLM is available.
    """

    def __init__(
        self,
        classifier: FallbackIntentClassifier,
        registry: FallbackResponseTemplateRegistry,
        preserve_input_in_unavailable: bool = True,
    ):
        self._classifier = classifier
        self._registry = registry
        self._preserve_input = preserve_input_in_unavailable

    def select(
        self,
        signal: LLMAvailabilitySignal,
        user_input: str,
    ) -> Optional[str]:
        if not signal.is_fallback_required():
            return None

        intent = self._classifier.classify(user_input)

        if intent != FallbackIntent.UNKNOWN:
            return self._registry.render(intent.value)

        if self._preserve_input:
            return self._registry.render(
                "unavailable_with_retry",
                user_input=user_input[:200],
            )

        return self._registry.render("unavailable_generic")
```

## Solution 5: LLM Availability Tracker

```python
import time
from threading import Lock
from typing import Optional


class LLMAvailabilityTracker:
    """
    Tracks LLM call outcomes and maintains the current availability signal.
    Transitions to UNAVAILABLE after N consecutive failures and recovers
    on first success.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_on_first_success: bool = True,
    ):
        self._threshold = failure_threshold
        self._recovery = recovery_on_first_success
        self._consecutive_failures = 0
        self._state = LLMAvailabilityState.UNKNOWN
        self._lock = Lock()

    def record_success(self, provider: str = "") -> LLMAvailabilitySignal:
        with self._lock:
            self._consecutive_failures = 0
            self._state = LLMAvailabilityState.AVAILABLE
            return LLMAvailabilitySignal(
                state=self._state,
                provider=provider,
                consecutive_failures=0,
            )

    def record_failure(self, error_type: str = "", provider: str = "") -> LLMAvailabilitySignal:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._state = LLMAvailabilityState.UNAVAILABLE
            else:
                self._state = LLMAvailabilityState.DEGRADED
            return LLMAvailabilitySignal(
                state=self._state,
                error_type=error_type,
                provider=provider,
                consecutive_failures=self._consecutive_failures,
            )

    def current_signal(self) -> LLMAvailabilitySignal:
        with self._lock:
            return LLMAvailabilitySignal(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
            )
```

## Solution 6: Fallback Response Pipeline

```python
import time
from typing import Any, Callable, Optional


class FallbackResponsePipeline:
    """
    Wraps LLM calls with fallback response selection.
    On LLM failure, records the failure and returns a template response
    instead of propagating the exception.
    """

    def __init__(
        self,
        tracker: LLMAvailabilityTracker,
        selector: FallbackResponseSelector,
        provider: str = "",
    ):
        self._tracker = tracker
        self._selector = selector
        self._provider = provider
        self._fallback_activations = 0

    async def call(
        self,
        llm_fn: Callable,
        user_input: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        try:
            result = await llm_fn(*args, **kwargs)
            self._tracker.record_success(self._provider)
            return {"response": result, "fallback_used": False}
        except Exception as exc:
            error_type = type(exc).__name__
            signal = self._tracker.record_failure(error_type, self._provider)
            fallback_text = self._selector.select(signal, user_input)

            if fallback_text:
                self._fallback_activations += 1
                return {
                    "response": fallback_text,
                    "fallback_used": True,
                    "fallback_reason": error_type,
                    "availability_state": signal.state.value,
                }

            raise

    def stats(self) -> dict:
        return {
            "fallback_activations": self._fallback_activations,
            "current_state": self._tracker.current_signal().state.value,
        }
```

## Comparison

| Approach | Intent Classification | Template Rendering | Availability Tracking | Fallback Activation | Input Preservation |
|---|---|---|---|---|---|
| FallbackIntentClassifier | Yes (regex) | No | No | No | No |
| FallbackResponseTemplateRegistry | No | Yes (format) | No | No | No |
| FallbackResponseSelector | Via classifier | Via registry | Via signal | Yes | Yes |
| LLMAvailabilityTracker | No | No | Yes | No | No |
| FallbackResponsePipeline | Via selector | Via selector | Via tracker | Yes | Via selector |

**Best for production**: Set `failure_threshold=3` so a single transient error does not activate fallback mode — but three consecutive failures almost certainly indicate an outage. Use `unavailable_with_retry` template (with preserved input) for all UNKNOWN intents so users can simply resend their message after recovery without re-typing. Register domain-specific fallback templates via `FallbackResponseTemplateRegistry.register()` for common intents in your product vertical. Monitor `fallback_activations` as a leading indicator of LLM provider health — a spike here precedes user-facing SLO breaches.
