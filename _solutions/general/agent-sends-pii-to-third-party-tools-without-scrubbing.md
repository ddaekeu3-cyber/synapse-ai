---
layout: solution
title: "Agent Sends PII to Third-Party Tools Without Scrubbing"
category: general
description: "Agent forwards user messages containing emails, SSNs, credit card numbers, or phone numbers directly to external APIs, logging services, or analytics tools — creating data compliance violations and privacy risks."
tags: [security, privacy, pii, gdpr, compliance, data-handling]
---

## Symptom

A support agent receives a user message and forwards it verbatim to a third-party sentiment analysis API:

```python
user_message = "My credit card 4111-1111-1111-1111 was charged twice. My SSN is 123-45-6789."
sentiment_api.analyze(text=user_message)  # ← PII sent to external service
```

The third-party vendor stores request logs. The user's credit card number and SSN now exist in an external system the company has no control over — a GDPR/CCPA violation, and potentially a PCI-DSS breach.

## Root Cause

The agent pipeline passes raw user input directly to tool calls without a scrubbing layer:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: raw user input forwarded to tools
def analyze_sentiment(text: str) -> dict:
    # text may contain PII — never sanitized
    return third_party_api.analyze(text=text)
```

---

## Fix

### Option 1 — Regex-based PII redaction before any external call

Apply a regex scrubber to all text before it leaves the agent's trust boundary.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# PII patterns — extend as needed for your jurisdiction
PII_PATTERNS = [
    # Credit card numbers (Visa, Mastercard, Amex, Discover)
    (re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b'), "[CARD]"),
    # SSNs: 123-45-6789 or 123456789
    (re.compile(r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b'), "[SSN]"),
    # Email addresses
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'), "[EMAIL]"),
    # US phone numbers
    (re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'), "[PHONE]"),
    # Passport-like numbers
    (re.compile(r'\b[A-Z]{1,2}\d{6,9}\b'), "[PASSPORT]"),
    # IP addresses
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), "[IP]"),
]


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """
    Replace PII with placeholder tokens.
    Returns (scrubbed_text, list_of_found_pii_types).
    """
    scrubbed = text
    found_types = []
    for pattern, replacement in PII_PATTERNS:
        if pattern.search(scrubbed):
            found_types.append(replacement)
            scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed, found_types


def safe_external_call(user_text: str, external_fn) -> dict:
    """Scrub PII before forwarding to any external function."""
    scrubbed, found = scrub_pii(user_text)

    if found:
        print(f"[pii-guard] Redacted: {found} before external call")

    return external_fn(scrubbed)


# Simulate external tool
def analyze_sentiment(text: str) -> dict:
    return {"sentiment": "negative", "text_received": text[:50]}


user_input = "My card 4111-1111-1111-1111 was charged. Email me at alice@example.com."
result = safe_external_call(user_input, analyze_sentiment)
print(result)

# Expected Token Savings: PII never enters external logs → zero compliance-driven incident response
# Environment: any agent forwarding user messages to third-party APIs (analytics, CRM, search, logging)
```

---

### Option 2 — LLM-based PII detection for nuanced cases

Use a small, local-trust-boundary model to detect PII that regex can't catch (e.g., "my social is zero one two dash forty-five dash six seven eight nine").

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def llm_detect_pii(text: str) -> dict:
    """
    Use Claude to identify PII in text and return a scrubbed version.
    This call stays within the trusted boundary (Anthropic's API, not a third party).
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="""Detect and redact PII in the provided text.
PII includes: names, emails, phone numbers, SSNs, credit card numbers, addresses, dates of birth,
passport numbers, bank account numbers, and any other personally identifiable information.

Return JSON:
{
  "scrubbed_text": "text with PII replaced by [TYPE] tokens",
  "pii_found": ["EMAIL", "SSN", ...],
  "has_pii": true/false
}

Replace each PII instance with the most specific type tag possible (e.g., [SSN] not [PII]).""",
        messages=[{"role": "user", "content": f"Scrub PII from:\n\n{text}"}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {"scrubbed_text": text, "pii_found": [], "has_pii": False}


def safe_send_to_crm(user_message: str, crm_api_fn) -> dict:
    """Detect and scrub PII before CRM submission."""
    result = llm_detect_pii(user_message)

    if result["has_pii"]:
        print(f"[pii-llm] Detected: {result['pii_found']}")
        safe_text = result["scrubbed_text"]
    else:
        safe_text = user_message

    return crm_api_fn(safe_text)


def mock_crm(text: str) -> dict:
    return {"logged": True, "preview": text[:60]}


msg = "Hi, I'm John Smith. My account email is john.smith@corp.com and DOB is 03/15/1985."
outcome = safe_send_to_crm(msg, mock_crm)
print(outcome)

# Expected Token Savings: LLM scrubbing catches natural-language PII that regex misses → no regulatory fines
# Environment: support bots, CRM integrations, compliance-sensitive industries (healthcare, finance)
```

---

### Option 3 — Data classification before tool routing

Classify the sensitivity of each user turn before deciding which tools can receive it. Sensitive turns skip third-party tools entirely.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

SENSITIVITY_LEVELS = {
    "public": ["all_tools"],
    "internal": ["internal_tools", "trusted_apis"],
    "confidential": ["internal_tools"],
    "restricted": [],  # No external forwarding allowed
}


def classify_sensitivity(text: str) -> str:
    """Classify text sensitivity level."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system="""Classify the sensitivity of this text.
Return exactly one of: "public", "internal", "confidential", "restricted"

restricted: contains SSN, credit card, health records, passwords, biometric data
confidential: contains email, phone, full name + company, financial figures
internal: contains company-specific info but no personal data
public: no personal or sensitive information""",
        messages=[{"role": "user", "content": text}]
    )
    level = response.content[0].text.strip().lower()
    return level if level in SENSITIVITY_LEVELS else "confidential"


class SensitivityAwareRouter:
    def __init__(self):
        self._tools: dict[str, dict] = {
            "sentiment_api": {"category": "trusted_apis", "description": "Third-party sentiment"},
            "crm_api": {"category": "trusted_apis", "description": "Third-party CRM"},
            "internal_classifier": {"category": "internal_tools", "description": "Internal ML model"},
            "internal_logger": {"category": "internal_tools", "description": "Internal audit log"},
        }

    def allowed_tools(self, sensitivity: str) -> list[str]:
        """Return tools allowed for a given sensitivity level."""
        allowed_categories = set(SENSITIVITY_LEVELS.get(sensitivity, []))
        return [
            name for name, meta in self._tools.items()
            if meta["category"] in allowed_categories or "all_tools" in allowed_categories
        ]

    def route(self, text: str, requested_tool: str) -> dict:
        sensitivity = classify_sensitivity(text)
        allowed = self.allowed_tools(sensitivity)

        print(f"[router] Sensitivity: {sensitivity}, Allowed: {allowed}")

        if requested_tool not in allowed:
            return {
                "error": f"Tool '{requested_tool}' not allowed for {sensitivity} content",
                "sensitivity": sensitivity,
                "allowed_tools": allowed,
                "action": "Use an internal tool or scrub the content first"
            }

        # In production: call the actual tool
        return {"routed_to": requested_tool, "sensitivity": sensitivity, "text_preview": text[:30] + "..."}


router = SensitivityAwareRouter()

# Restricted content attempting to reach third-party
print(router.route("My SSN is 123-45-6789 and card is 4111111111111111", "sentiment_api"))
print()
# Public content — all tools available
print(router.route("I love the new product interface!", "sentiment_api"))

# Expected Token Savings: blocked routing prevents PII reaching external APIs; avoids incident response costs
# Environment: multi-tool agents; compliance-gated data pipelines; enterprise deployments
```

---

### Option 4 — PII vault: replace before sending, restore before displaying

Replace PII tokens with opaque IDs before any external call, then restore them in the final response shown to the user.

```python
import anthropic
import re
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class PiiVault:
    """Tokenise PII before external calls; detokenise for user-facing output."""
    _tokens: dict[str, str] = field(default_factory=dict)  # token → original value
    _reverse: dict[str, str] = field(default_factory=dict)  # original → token

    def tokenise(self, text: str) -> str:
        """Replace PII values with opaque tokens. Same value always gets same token."""
        result = text
        patterns = [
            re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),         # Email
            re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),       # Phone
            re.compile(r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b'),                                 # SSN
        ]
        for pattern in patterns:
            for match in pattern.finditer(result):
                original = match.group(0)
                if original not in self._reverse:
                    token = f"[TOK_{uuid.uuid4().hex[:8].upper()}]"
                    self._tokens[token] = original
                    self._reverse[original] = token
                result = result.replace(original, self._reverse[original])
        return result

    def detokenise(self, text: str) -> str:
        """Restore original PII values from tokens."""
        result = text
        for token, original in self._tokens.items():
            result = result.replace(token, original)
        return result


vault = PiiVault()


def process_user_request(user_text: str, external_api_fn) -> str:
    """Tokenise → external call → detokenise."""
    tokenised = vault.tokenise(user_text)
    print(f"[vault] Sent externally: {tokenised}")

    # External API receives only tokens, never real PII
    external_response = external_api_fn(tokenised)

    # Detokenise the response so the user sees real values
    return vault.detokenise(external_response)


def mock_classifier(text: str) -> str:
    """Simulated external classifier — only sees tokens."""
    return f"Classification: COMPLAINT | Input preview: {text[:50]}"


user_msg = "I need help. My email is alice@corp.com and my phone is (555) 123-4567."
response = process_user_request(user_msg, mock_classifier)
print(f"[response to user] {response}")

# Expected Token Savings: PII stays internal; no compliance remediation cost from leaks
# Environment: agents that must pass content to external APIs while maintaining GDPR/CCPA compliance
```

---

### Option 5 — Tool-level PII policy annotations

Annotate each tool definition with its PII policy. The agent framework enforces scrubbing automatically before calling any tool marked `pii_policy: scrub`.

```python
import anthropic
import re
import json
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
PHONE_RE = re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
SSN_RE = re.compile(r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b')


def scrub(text: str) -> str:
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = SSN_RE.sub("[SSN]", text)
    return text


# Tool registry with PII policies
TOOL_REGISTRY: dict[str, dict] = {
    "analyze_sentiment": {
        "pii_policy": "scrub",           # Third-party — scrub all PII
        "trust_level": "external",
        "fn": lambda text: json.dumps({"sentiment": "negative", "text": text[:40]}),
    },
    "log_to_audit": {
        "pii_policy": "allowed",          # Internal — PII permitted
        "trust_level": "internal",
        "fn": lambda text: json.dumps({"logged": True}),
    },
    "search_knowledge_base": {
        "pii_policy": "scrub",           # External search engine — scrub
        "trust_level": "external",
        "fn": lambda query: json.dumps({"results": ["Article 1", "Article 2"]}),
    },
}

TOOLS_SPEC = [
    {
        "name": name,
        "description": f"Tool with {meta['pii_policy']} PII policy ({meta['trust_level']})",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "query": {"type": "string"}},
        }
    }
    for name, meta in TOOL_REGISTRY.items()
]


def dispatch_tool(name: str, input_data: dict) -> str:
    """Dispatch tool call, enforcing PII policy automatically."""
    meta = TOOL_REGISTRY.get(name)
    if not meta:
        return json.dumps({"error": f"Unknown tool: {name}"})

    policy = meta["pii_policy"]
    text_field = input_data.get("text") or input_data.get("query", "")

    if policy == "scrub" and text_field:
        scrubbed = scrub(text_field)
        if scrubbed != text_field:
            print(f"[pii-policy] Scrubbed PII for tool '{name}'")
        safe_input = {**input_data, "text": scrubbed, "query": scrubbed}
    else:
        safe_input = input_data

    call_input = safe_input.get("text") or safe_input.get("query", "")
    return meta["fn"](call_input)


def run_pii_safe_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS_SPEC,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = dispatch_tool(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = run_pii_safe_agent("Analyze sentiment of: 'My card 4111111111111111 was declined. Email me at bob@example.com'")
print(result)

# Expected Token Savings: policy-based dispatch is zero-overhead; PII scrubbing is ~microseconds per call
# Environment: multi-tool agents mixing internal and external APIs; compliance-requiring enterprise deployments
```

---

### Option 6 — Output scanning: detect if model response leaks PII from tool results

Scan the model's final response before showing it to the user, in case the model accidentally echoes back PII from tool outputs.

```python
import anthropic
import re
import json

client = anthropic.Anthropic(api_key="sk-live-...")

PII_SCAN_PATTERNS = {
    "EMAIL": re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),
    "PHONE": re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
    "SSN": re.compile(r'\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b'),
    "CARD": re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14})\b'),
}


def scan_for_pii(text: str) -> dict[str, list[str]]:
    """Detect PII in output text. Returns {type: [matches]}."""
    found = {}
    for pii_type, pattern in PII_SCAN_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            found[pii_type] = matches
    return found


def redact_output(text: str) -> tuple[str, dict]:
    """Redact any PII from the model's output before showing to user."""
    redacted = text
    found = {}
    for pii_type, pattern in PII_SCAN_PATTERNS.items():
        matches = pattern.findall(redacted)
        if matches:
            found[pii_type] = matches
            redacted = pattern.sub(f"[{pii_type}]", redacted)
    return redacted, found


def run_with_output_scan(user_message: str) -> str:
    """Run agent and scan final output for leaked PII before delivery."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": user_message}]
    )
    raw_output = response.content[0].text.strip()

    # Scan for PII before returning to caller
    redacted, leaks = redact_output(raw_output)

    if leaks:
        print(f"[output-scan] WARNING: Model leaked PII — redacted: {list(leaks.keys())}")
        return redacted

    return raw_output


# Test with a scenario that might cause the model to echo sensitive data
result = run_with_output_scan(
    "A user reported their email alice@example.com and SSN 987-65-4321 "
    "were used without consent. Summarise the complaint."
)
print(result)

# Expected Token Savings: output scan prevents regulatory exposure; catches model leakage before user sees it
# Environment: agents that process PII-containing inputs and might echo back sensitive fields
```

---

## Comparison

| Option | Prevents External Transmission | Catches Nuanced PII | Performance Impact | Complexity |
|--------|-------------------------------|--------------------|--------------------|------------|
| 1 | Yes (regex) | No | Negligible | Low |
| 2 | Yes (LLM) | Yes | Medium (extra call) | Low |
| 3 | Yes (routing) | Partial | Medium (extra call) | Medium |
| 4 | Yes (tokenise) | No | Negligible | Medium |
| 5 | Yes (policy) | No | Negligible | Low |
| 6 | Detection only | No | Negligible | Low |

**Recommended starting point:** Option 1 (regex scrubber) as a first layer — cover the most common PII patterns and apply it to all text before any external tool call. Add Option 5's policy annotation to the tool registry so new tools are classified at definition time. Use Option 2 (LLM detection) for content where users may write out PII in natural language (e.g., "my social is...") and regex would miss it.
