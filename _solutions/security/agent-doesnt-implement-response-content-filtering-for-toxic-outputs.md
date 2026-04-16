---
title: "Agent Doesn't Implement Response Content Filtering for Toxic Outputs"
description: "Agents that return LLM responses directly to users without content filtering can deliver harmful, offensive, or policy-violating content when the model is manipulated via prompt injection, jailbreak attempts, or adversarial inputs. Implement response content filtering that scans generated text for toxic categories before delivery, applies graduated response policies, and logs all filter activations for policy review."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-response-content-filtering-for-toxic-outputs
tags: [content-filtering, toxic-output, response-safety, jailbreak-defense, output-moderation, harm-prevention]
symptoms:
  - "Prompt injection causes the agent to output harmful or offensive content"
  - "Jailbreak attempts succeed because responses are not filtered before delivery"
  - "No policy for what to return when the LLM produces inappropriate content"
  - "Content policy violations reach users before the team discovers them in logs"
  - "No classification of response content into harmful categories before delivery"
---

## Why This Happens

LLMs can be manipulated into producing harmful outputs through prompt injection, jailbreaking, or adversarial inputs embedded in retrieved documents. Without a post-generation filter, every harmful output reaches the user. Response filtering requires classifying generated text against a set of harm categories, applying a graduated policy (warn, replace, block), and ensuring that the filtering layer cannot itself be bypassed by clever prompt construction. The filter must run on the final response text, not on the prompt, to catch harm introduced by the model regardless of how it was triggered.

## Solution 1: Content Harm Category

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Pattern
import re


class HarmCategory(str, Enum):
    HATE_SPEECH = "hate_speech"
    VIOLENCE = "violence"
    SELF_HARM = "self_harm"
    SEXUAL = "sexual"
    DANGEROUS_INSTRUCTIONS = "dangerous_instructions"
    PII_LEAK = "pii_leak"
    PROMPT_INJECTION_ECHO = "prompt_injection_echo"


class FilterAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"          # deliver with a warning prepended
    REPLACE = "replace"    # substitute a safe fallback message
    BLOCK = "block"        # return an error; do not deliver


@dataclass
class ContentFilterRule:
    category: HarmCategory
    patterns: List[str]         # regex patterns; any match triggers this rule
    action: FilterAction
    severity: int = 1           # 1=low, 2=medium, 3=high
    replacement_message: str = "I'm unable to provide that information."
```

## Solution 2: Default Content Filter Rule Set

```python
from typing import List


def default_content_filter_rules() -> List[ContentFilterRule]:
    return [
        ContentFilterRule(
            category=HarmCategory.DANGEROUS_INSTRUCTIONS,
            patterns=[
                r"\bhow to (make|build|create|synthesize) (a bomb|explosives|poison|malware)\b",
                r"\bstep[s]? (to|for) (hack|exploit|bypass) \w+",
                r"\binstructions? (for|to) (kill|harm|attack) \w+",
            ],
            action=FilterAction.BLOCK,
            severity=3,
        ),
        ContentFilterRule(
            category=HarmCategory.HATE_SPEECH,
            patterns=[
                r"\b(slur1|slur2|slur3)\b",   # replace with actual slurs in production
                r"\b(all|every) \w+ (should|must) (die|be killed|be eliminated)\b",
            ],
            action=FilterAction.REPLACE,
            severity=3,
        ),
        ContentFilterRule(
            category=HarmCategory.PII_LEAK,
            patterns=[
                r"\b\d{3}-\d{2}-\d{4}\b",             # SSN
                r"\b4[0-9]{12}(?:[0-9]{3})?\b",        # Visa card number
                r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",  # email
            ],
            action=FilterAction.WARN,
            severity=2,
        ),
        ContentFilterRule(
            category=HarmCategory.PROMPT_INJECTION_ECHO,
            patterns=[
                r"ignore (all |previous )?instructions",
                r"you are now (DAN|an AI without restrictions|jailbroken)",
                r"pretend you (have no|don't have) (restrictions|filters|guidelines)",
            ],
            action=FilterAction.REPLACE,
            severity=2,
        ),
    ]
```

## Solution 3: Response Content Classifier

```python
import re
from typing import List, Tuple


class ResponseContentClassifier:
    """
    Scans a response text against all registered filter rules and
    returns the set of triggered rules sorted by severity.
    """

    def __init__(self, rules: List[ContentFilterRule]):
        self._rules = rules
        self._compiled = [
            (rule, [re.compile(p, re.IGNORECASE | re.DOTALL) for p in rule.patterns])
            for rule in rules
        ]

    def classify(self, text: str) -> List[ContentFilterRule]:
        triggered = []
        for rule, patterns in self._compiled:
            for pattern in patterns:
                if pattern.search(text):
                    triggered.append(rule)
                    break
        return sorted(triggered, key=lambda r: r.severity, reverse=True)
```

## Solution 4: Response Filter Policy Engine

```python
import re
from typing import List, Optional


class ResponseFilterPolicyEngine:
    """
    Applies the highest-severity triggered rule's action to the response.
    For WARN: prepends a warning notice.
    For REPLACE: substitutes a safe fallback message.
    For BLOCK: raises FilterBlockedError.
    """

    WARNING_PREFIX = "[Content notice: This response may contain sensitive information.]\n\n"

    def apply(
        self,
        text: str,
        triggered_rules: List[ContentFilterRule],
    ) -> dict:
        if not triggered_rules:
            return {
                "text": text,
                "action_taken": FilterAction.ALLOW.value,
                "triggered_categories": [],
            }

        highest = triggered_rules[0]  # already sorted by severity desc
        categories = [r.category.value for r in triggered_rules]

        if highest.action == FilterAction.BLOCK:
            raise FilterBlockedError(
                f"Response blocked: {highest.category.value}",
                categories=categories,
            )

        if highest.action == FilterAction.REPLACE:
            return {
                "text": highest.replacement_message,
                "action_taken": FilterAction.REPLACE.value,
                "triggered_categories": categories,
            }

        if highest.action == FilterAction.WARN:
            return {
                "text": self.WARNING_PREFIX + text,
                "action_taken": FilterAction.WARN.value,
                "triggered_categories": categories,
            }

        return {
            "text": text,
            "action_taken": FilterAction.ALLOW.value,
            "triggered_categories": categories,
        }


class FilterBlockedError(Exception):
    def __init__(self, message: str, categories: List[str]):
        super().__init__(message)
        self.categories = categories
```

## Solution 5: Filtered Response Gateway

```python
import time
from typing import Callable, List, Optional


class FilteredResponseGateway:
    """
    Wraps LLM response delivery with content classification and
    policy enforcement. Logs all filter activations for audit.
    """

    def __init__(
        self,
        classifier: ResponseContentClassifier,
        policy: ResponseFilterPolicyEngine,
    ):
        self._classifier = classifier
        self._policy = policy
        self._filter_log: List[dict] = []
        self._block_count = 0
        self._replace_count = 0
        self._warn_count = 0
        self._allow_count = 0

    def process(
        self,
        response_text: str,
        session_id: str = "",
    ) -> dict:
        triggered = self._classifier.classify(response_text)
        try:
            result = self._policy.apply(response_text, triggered)
        except FilterBlockedError as exc:
            self._block_count += 1
            self._filter_log.append({
                "ts": time.time(),
                "session_id": session_id,
                "action": "block",
                "categories": exc.categories,
            })
            return {
                "text": "I'm unable to provide a response to that request.",
                "action_taken": "block",
                "triggered_categories": exc.categories,
                "blocked": True,
            }

        action = result["action_taken"]
        if action == FilterAction.REPLACE.value:
            self._replace_count += 1
        elif action == FilterAction.WARN.value:
            self._warn_count += 1
        else:
            self._allow_count += 1

        if action != FilterAction.ALLOW.value:
            self._filter_log.append({
                "ts": time.time(),
                "session_id": session_id,
                "action": action,
                "categories": result["triggered_categories"],
            })

        return {**result, "blocked": False}

    def stats(self) -> dict:
        total = self._allow_count + self._warn_count + self._replace_count + self._block_count
        return {
            "total_responses": total,
            "allowed": self._allow_count,
            "warned": self._warn_count,
            "replaced": self._replace_count,
            "blocked": self._block_count,
            "filter_activation_rate": round(
                (total - self._allow_count) / max(total, 1), 4
            ),
        }
```

## Solution 6: Content Filter Audit Dashboard

```python
import time
from typing import Dict


class ContentFilterAuditDashboard:
    """
    Aggregates filter activation statistics and surfaces
    category-level breakdown for policy review.
    """

    def __init__(self, gateway: FilteredResponseGateway):
        self._gateway = gateway

    def render(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent_log = [e for e in self._gateway._filter_log if e["ts"] >= cutoff]
        by_category: Dict[str, int] = {}
        for entry in recent_log:
            for cat in entry.get("categories", []):
                by_category[cat] = by_category.get(cat, 0) + 1
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "stats": self._gateway.stats(),
            "activations_in_window": len(recent_log),
            "by_category": by_category,
        }
```

## Comparison

| Approach | Pattern Detection | Graduated Actions | Block/Replace/Warn | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| ResponseContentClassifier | Yes (regex) | No | No | No | No |
| ResponseFilterPolicyEngine | No | Yes (severity) | Yes | No | No |
| FilteredResponseGateway | Via classifier | Via policy | Via policy | Yes | No |
| ContentFilterAuditDashboard | No | No | No | Via gateway | Yes |

**Best for production**: Layer this filter after any provider-side safety filtering, not instead of it — provider filters catch broad harm categories but not domain-specific policy violations like PII leakage from retrieved documents. Use `FilterAction.REPLACE` rather than `BLOCK` for most categories; a blocked response with no explanation frustrates users and provides no signal to the LLM about what was wrong. Reserve `BLOCK` for dangerous-instruction categories where even acknowledging the content could cause harm. Monitor `by_category` weekly — a spike in `prompt_injection_echo` activations indicates that adversarial documents are reaching the retrieval pipeline and the document ingestion layer needs stronger pre-processing.
