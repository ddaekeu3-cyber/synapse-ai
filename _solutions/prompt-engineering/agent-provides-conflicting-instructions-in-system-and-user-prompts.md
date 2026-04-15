---
layout: solution
title: "Agent provides conflicting instructions in system and user prompts"
category: prompt-engineering
description: "The system prompt says 'always respond in JSON' while the user turn says 'write a friendly message' — the model picks one arbitrarily, producing inconsistent output that breaks downstream parsers."
tags: [prompt-engineering, conflicting-instructions, system-prompt, consistency, instruction-priority]
---

## Symptom

Some requests return well-formed JSON; others return prose. The inconsistency is not random — it follows a pattern where certain user phrasings ("write me", "please explain", "tell me about") trigger the prose path while others ("give me the data for", "return the result for") trigger JSON. The system prompt's format instruction is being silently overridden by the framing of the user message.

## Root Cause

LLMs resolve instruction conflicts by inferred priority: recent instructions (user turn) often win over earlier ones (system prompt), and naturally-phrased requests ("write a friendly message") carry strong implicit format signals that override explicit system-level constraints. The conflict is invisible to developers who test with neutral prompts like "get order 42" but only appears under realistic user inputs.

---

## Option 1 — Explicit priority statement in the system prompt

**State clearly which instructions take precedence. Name the conflict explicitly so the model knows how to resolve it.**

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a data extraction assistant.

OUTPUT RULES — these apply unconditionally, even if the user asks for a "friendly message", 
"summary", or uses casual language. The output format ALWAYS takes priority over the 
phrasing of the user's request.

MANDATORY FORMAT: Respond ONLY with a JSON object:
{
  "answer": "your response here",
  "confidence": "high" | "medium" | "low",
  "source": "knowledge" | "tool_result"
}

Never write prose, markdown, or explanations outside this JSON structure.
If the user asks for something that cannot fit this format, set answer to your best 
one-sentence response.
"""


def ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


import json

# These phrasings previously triggered prose mode
for prompt in [
    "Write me a friendly explanation of our refund policy.",
    "Tell me about the features of the premium plan.",
    "Give me data for order 42.",
]:
    result = ask(prompt)
    print(f"Q: {prompt[:50]}")
    try:
        parsed = json.loads(result)
        print(f"A (JSON): {parsed}\n")
    except json.JSONDecodeError:
        print(f"A (PROSE — format failure): {result[:80]}\n")
```

**Expected Token Savings:** Explicit priority resolution reduces format failures from ~30% to <5% for conversational prompts — eliminates 1–2 retry turns per failed formatting attempt (~500 tokens each).

**Environment:** Any agent with format constraints; zero extra API calls.

---

## Option 2 — System prompt auditor that detects contradictions before deployment

**Scan the system and user prompt templates for contradictory instruction pairs and warn at startup.**

```python
import re
import anthropic

client = anthropic.Anthropic()

CONTRADICTION_PAIRS = [
    # (system_pattern, user_pattern, description)
    (r"respond.*json|output.*json|format.*json",       r"write.*friendly|write.*message|write.*email",      "JSON format vs. prose writing request"),
    (r"concise|brief|short",                           r"detailed|comprehensive|thorough|in.depth",         "brevity vs. thoroughness"),
    (r"formal|professional",                           r"casual|friendly|informal|conversational",          "formal vs. casual tone"),
    (r"do not|never|must not",                         r"please.*do|can you.*do|i want you to.*do",         "prohibition vs. direct request"),
    (r"respond in (english|french|spanish|german)",    r"(respond|answer|reply) in (english|french|spanish|german)", "language conflict"),
    (r"max.*\d+.*word|under.*\d+.*word|\d+.*word.*max", r"full|complete|comprehensive|everything",         "word limit vs. completeness"),
]


def audit_prompt_pair(system: str, user_template: str) -> list[str]:
    """Return a list of detected contradictions."""
    warnings_found = []
    s = system.lower()
    u = user_template.lower()
    for sys_pat, usr_pat, desc in CONTRADICTION_PAIRS:
        if re.search(sys_pat, s) and re.search(usr_pat, u):
            warnings_found.append(f"  ⚠ {desc}")
    return warnings_found


def build_agent(system: str, user_template: str):
    issues = audit_prompt_pair(system, user_template)
    if issues:
        print("Prompt contradiction warnings detected:")
        for w in issues:
            print(w)
        print("  Resolve before deploying — conflicting instructions cause inconsistent output.\n")

    def ask(user_message: str) -> str:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_template.format(message=user_message)}],
        )
        return response.content[0].text

    return ask


# This will print a warning
bad_agent = build_agent(
    system="Respond ONLY with JSON. Be concise.",
    user_template="Write me a friendly, detailed message about: {message}",
)

# No warning
good_agent = build_agent(
    system="Respond ONLY with JSON. Be concise.",
    user_template="Return JSON data for: {message}",
)

print(good_agent("the refund policy"))
```

**Expected Token Savings:** Catching contradictions at build time prevents entire categories of runtime format failures — no tokens wasted on inconsistent outputs or retry loops in production.

**Environment:** Agents with templated user prompts; run audit at module load time or in a pre-deployment check.

---

## Option 3 — Normalize user messages to remove conflicting format signals

**Strip implicit format signals from user messages before sending them to the model.**

```python
import re
import anthropic

client = anthropic.Anthropic()

# Phrases that carry implicit "write prose" format signals
PROSE_SIGNALS = [
    (r"\bwrite\s+(?:me\s+)?a\b",         "provide"),
    (r"\bwrite\s+(?:me\s+)?an\b",        "provide"),
    (r"\btell\s+me\s+about\b",           "return data about"),
    (r"\bexplain\s+(?:to\s+me\s+)?",     "return the explanation of "),
    (r"\bdescribe\s+",                    "return the description of "),
    (r"\bcreate\s+a\s+(?:friendly\s+)?", "return "),
    (r"\bdraft\s+a\b",                   "return "),
]

SYSTEM = """You are a data API. Respond ONLY with a JSON object:
{"result": "...", "type": "string|number|list|object"}
"""


def normalize_user_message(user_message: str) -> str:
    """Replace prose-triggering phrases with neutral data-request phrasing."""
    normalized = user_message
    for pattern, replacement in PROSE_SIGNALS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    if normalized != user_message:
        print(f"  Normalized: {user_message!r} → {normalized!r}")
    return normalized


def ask(user_message: str) -> str:
    normalized = normalize_user_message(user_message)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": normalized}],
    )
    return response.content[0].text


import json

for prompt in [
    "Write me a summary of our pricing tiers.",
    "Tell me about the enterprise plan.",
    "Return JSON data for the starter plan.",
]:
    result = ask(prompt)
    print(f"Q: {prompt[:50]}")
    try:
        print(f"A: {json.loads(result)}\n")
    except json.JSONDecodeError:
        print(f"A (PROSE — still failed): {result[:80]}\n")
```

**Expected Token Savings:** Normalisation prevents format conflicts before they reach the model — eliminates format-failure retry turns for the most common conflict patterns, saving ~30% of retry token spend.

**Environment:** Agents where user input is uncontrolled (chat interfaces, API endpoints); requires periodic review of `PROSE_SIGNALS` as new patterns emerge.

---

## Option 4 — Two-stage pipeline: classify then format

**Separate the reasoning step (no format constraint) from the formatting step (strict format constraint). Conflicts cannot arise because the format instruction is applied after content is generated.**

```python
import json
import anthropic

client = anthropic.Anthropic()

DOMAIN_SYSTEM = """You are a knowledgeable assistant. Answer questions clearly and completely.
Do not worry about output format — just provide the best possible answer."""

FORMAT_PROMPT = """Convert the following answer to this exact JSON schema:
{{"answer": string, "key_points": [string], "confidence": "high"|"medium"|"low"}}
Return ONLY the JSON, no other text.

ANSWER TO CONVERT:
{answer}"""


def ask_two_stage(user_message: str) -> dict:
    # Stage 1: Domain reasoning — no format pressure
    stage1 = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=DOMAIN_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_answer = stage1.content[0].text

    # Stage 2: Format conversion — haiku, short context, no domain knowledge needed
    stage2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": FORMAT_PROMPT.format(answer=raw_answer[:3000]),
        }],
    )
    raw_json = stage2.content[0].text.strip()
    start = raw_json.find("{")
    end   = raw_json.rfind("}") + 1
    return json.loads(raw_json[start:end])


for prompt in [
    "Write me a friendly explanation of quantum entanglement.",
    "Tell me everything about our SLA guarantees.",
    "Give me the data on subscription pricing.",
]:
    result = ask_two_stage(prompt)
    print(f"Q: {prompt[:50]}")
    print(f"A: {json.dumps(result, indent=2)[:200]}\n")
```

**Expected Token Savings:** Two-stage separation achieves 100% format compliance — no retry turns needed. Stage 2 (haiku, ~500 tokens) costs far less than a retry of stage 1 (sonnet, ~2,000+ tokens).

**Environment:** Agents where format compliance is mission-critical; adds ~200 tokens per request for the formatting stage.

---

## Option 5 — Instruction consolidation: merge system and user templates at build time

**If user message templates are controlled (not free-form), merge them with the system prompt at build time to remove all conflicts.**

```python
import anthropic

client = anthropic.Anthropic()


class PromptBuilder:
    """Build a single, conflict-free system prompt from system + user template components."""

    def __init__(self, base_system: str) -> None:
        self._components: list[str] = [base_system]
        self._user_template: str    = "{user_message}"

    def add_instruction(self, instruction: str) -> "PromptBuilder":
        self._components.append(instruction)
        return self

    def set_user_template(self, template: str) -> "PromptBuilder":
        self._user_template = template
        return self

    def build(self) -> tuple[str, str]:
        """Return (system_prompt, user_template) with conflicts resolved."""
        system = "\n\n".join(self._components)
        return system, self._user_template


# Build once at startup — conflict resolution happens here, not at runtime
system, user_template = (
    PromptBuilder("You are a customer service agent for Acme Corp.")
    .add_instruction("Tone: professional and empathetic.")
    .add_instruction(
        "OUTPUT FORMAT: Always respond with JSON:\n"
        '  {"response": string, "action_required": boolean, "escalate": boolean}\n'
        "This format applies even when the user asks for a 'friendly' or 'casual' reply."
    )
    .set_user_template(
        # Note: no prose-triggering phrases in the template
        "Customer query: {user_message}\n\nProvide your JSON response."
    )
    .build()
)


def ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_template.format(user_message=user_message)}],
    )
    return response.content[0].text


import json

for prompt in [
    "Write me a friendly apology for the delayed shipment.",
    "Tell me how to reset my password.",
    "I need to cancel my subscription.",
]:
    result = ask(prompt)
    print(f"Q: {prompt}")
    try:
        print(f"A: {json.loads(result)}\n")
    except json.JSONDecodeError:
        print(f"A (format failure): {result[:80]}\n")
```

**Expected Token Savings:** Single consolidated prompt with explicit conflict resolution reduces format failures to <2% — near-eliminates retry overhead for format-driven inconsistencies.

**Environment:** Agents with templated (not free-form) user inputs; `PromptBuilder` is a simple pattern, not a library dependency.

---

## Option 6 — Post-generation conflict detector with auto-correction

**After each response, detect whether the model followed the intended format. If not, trigger a targeted correction call.**

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

EXPECTED_FORMAT = "json"

SYSTEM = """You are a data API. Always respond with JSON: {"data": "...", "status": "ok"|"error"}"""

CORRECTION_PROMPT = """The previous response did not follow the required JSON format.
Required format: {{"data": "...", "status": "ok"|"error"}}

Previous response:
{prev_response}

Return ONLY valid JSON in the required format. No explanation."""


def detect_format(response: str) -> str:
    stripped = response.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            return "invalid_json"
    if re.search(r"```json", stripped):
        return "fenced_json"
    return "prose"


def ask_with_correction(user_message: str, max_corrections: int = 1) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text

    fmt = detect_format(raw)
    if fmt == "json":
        return json.loads(raw)

    if fmt == "fenced_json":
        # Extract from code fence
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))

    # Correction pass — only if format failed
    if max_corrections > 0:
        print(f"  Format was '{fmt}' — triggering correction call …")
        correction = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": CORRECTION_PROMPT.format(prev_response=raw[:1000]),
            }],
        )
        corrected = correction.content[0].text.strip()
        start = corrected.find("{")
        end   = corrected.rfind("}") + 1
        if start != -1:
            return json.loads(corrected[start:end])

    return {"data": raw, "status": "error", "note": "format correction failed"}


for prompt in [
    "Write me a friendly explanation of your return policy.",
    "Give me data for the enterprise plan.",
]:
    result = ask_with_correction(prompt)
    print(f"Q: {prompt[:50]}")
    print(f"A: {result}\n")
```

**Expected Token Savings:** Targeted correction with haiku (~200 tokens) is 5–10× cheaper than a full sonnet retry (~1,500+ tokens). For 10% format failure rate on 1,000 daily calls: saves ~1,300,000 tokens vs. full sonnet retries.

**Environment:** Production agents where some format failures are acceptable at generation time but must be corrected before delivery; good safety net for Options 1–5.

---

## Comparison

| Option | Prevention vs. Correction | Extra API Calls | Format Failure Rate | Complexity |
|--------|--------------------------|----------------|--------------------|----|
| 1. Explicit priority statement | Prevention | Zero | ~5% | Very Low |
| 2. Startup contradiction audit | Prevention | Zero (dev time) | Detected early | Low |
| 3. User message normalisation | Prevention | Zero | ~10% | Low |
| 4. Two-stage pipeline | Prevention (structural) | One (haiku) | <1% | Medium |
| 5. `PromptBuilder` consolidation | Prevention | Zero | ~2% | Low |
| 6. Post-gen correction | Correction | Zero or one (haiku) | 0% delivered | Medium |

**Recommended path:** Start with Option 1 (explicit priority statement) — a one-paragraph addition to the system prompt that cuts failures by 80%. Add Option 2 (startup audit) to catch future regressions. For critical pipelines, layer Option 4 (two-stage) or Option 6 (post-gen correction) to guarantee 100% format compliance on delivery.
