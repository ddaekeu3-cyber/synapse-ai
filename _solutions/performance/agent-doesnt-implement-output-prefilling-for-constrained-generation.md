---
layout: solution
title: "Agent Doesn't Implement Output Prefilling for Constrained Generation"
category: performance
description: "Use assistant message prefilling to constrain output format, skip preamble, and reduce parsing overhead in structured generation tasks."
tags: [prefilling, constrained-generation, performance, structured-output, format-control]
---

# Agent Doesn't Implement Output Prefilling for Constrained Generation

## Problem

Without prefilling, models waste tokens on preamble ("Sure! Here's the JSON you requested..."), require complex post-processing to extract structured content, and may produce inconsistent format adherence across requests.

## Solution Options

### Option 1: Direct JSON Prefill to Skip Preamble

```python
import anthropic
import json

client = anthropic.Anthropic()

def extract_structured_data_without_prefill(text: str) -> dict:
    """Baseline: no prefill — model may add prose before JSON."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {"role": "user", "content": f"Extract person details as JSON with keys: name, age, occupation.\n\nText: {text}"}
        ]
    )
    raw = resp.content[0].text
    # Must parse JSON from potentially noisy output
    import re
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(match.group()) if match else {}

def extract_structured_data_with_prefill(text: str) -> dict:
    """With prefill: model continues from '{' — guaranteed JSON start."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {"role": "user", "content": f"Extract person details as JSON with keys: name, age, occupation.\n\nText: {text}"},
            {"role": "assistant", "content": "{"}  # prefill forces JSON-first output
        ]
    )
    # Response continues from '{', so prepend it back
    return json.loads("{" + resp.content[0].text)

sample = "Dr. Sarah Chen, 42, has been a neurosurgeon at Mass General for 15 years."

result_no_prefill = extract_structured_data_without_prefill(sample)
result_with_prefill = extract_structured_data_with_prefill(sample)

print("Without prefill:", result_no_prefill)
print("With prefill:", result_with_prefill)

# Expected Token Savings: ~20-40 tokens per request by eliminating "Sure! Here's the JSON:" preamble
# Environment: high-volume extraction pipelines, structured data APIs, form parsing
```

### Option 2: Markdown Code Block Prefill for Code Generation

```python
import anthropic

client = anthropic.Anthropic()

def generate_code_without_prefill(task: str, language: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write a {language} function to {task}"}]
    )
    text = resp.content[0].text
    # Strip prose wrapper if present
    import re
    match = re.search(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    return match.group(1).strip() if match else text

def generate_code_with_prefill(task: str, language: str) -> str:
    """Prefill with opening code fence — model starts writing code immediately."""
    fence_open = f"```{language}\n"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {"role": "user", "content": f"Write a {language} function to {task}. Only the function, no explanation."},
            {"role": "assistant", "content": fence_open}
        ]
    )
    # Trim closing fence if present
    code = resp.content[0].text
    if "```" in code:
        code = code[:code.rfind("```")].rstrip()
    return code

tasks = [
    ("binary search a sorted list", "python"),
    ("debounce a function call", "javascript"),
    ("parse ISO 8601 duration strings", "python")
]

for task, lang in tasks:
    code = generate_code_with_prefill(task, lang)
    print(f"[{lang}] {task}:\n{code[:150]}...\n")

# Expected Token Savings: ~15-30 tokens per request; eliminates "Here's a Python function that..."
# Environment: code generation services, IDE assistants, automated scaffolding tools
```

### Option 3: Schema-Locked Prefill for Consistent API Responses

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

# Define strict response schema
ANALYSIS_SCHEMA_PREFIX = '{"sentiment": "'  # forces sentiment field first
REVIEW_SCHEMA_PREFIX = '{"score": '          # forces numeric score first

def analyze_sentiment_prefilled(text: str) -> dict:
    """Prefill locks model into schema — eliminates format variation."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system='Respond only with valid JSON. Schema: {"sentiment": "positive|negative|neutral", "confidence": 0.0-1.0, "keywords": []}',
        messages=[
            {"role": "user", "content": f"Analyze: {text}"},
            {"role": "assistant", "content": ANALYSIS_SCHEMA_PREFIX}
        ]
    )
    return json.loads(ANALYSIS_SCHEMA_PREFIX + resp.content[0].text)

def score_review_prefilled(review: str) -> dict:
    """Prefill with '{"score": ' forces integer score output immediately."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system='Respond only with JSON. Schema: {"score": 1-10, "summary": "string", "recommend": true|false}',
        messages=[
            {"role": "user", "content": f"Score this product review: {review}"},
            {"role": "assistant", "content": REVIEW_SCHEMA_PREFIX}
        ]
    )
    return json.loads(REVIEW_SCHEMA_PREFIX + resp.content[0].text)

texts = [
    "The new MacBook Pro is absolutely incredible — best laptop I've ever owned.",
    "Totally disappointed. Battery life is worse than advertised and support was unhelpful.",
    "It's okay. Does the job but nothing special about it."
]

for text in texts:
    result = analyze_sentiment_prefilled(text)
    print(f"Sentiment: {result}")

reviews = [
    "Amazing product! Works exactly as described. Fast shipping. Will buy again.",
    "Broke after two weeks. Complete waste of money. Avoid."
]
for review in reviews:
    score = score_review_prefilled(review)
    print(f"Review score: {score}")

# Expected Token Savings: ~25% by forcing schema compliance without validation retry loops
# Environment: sentiment APIs, review scoring, any high-volume structured inference endpoint
```

### Option 4: Streaming Prefill with Early Termination

```python
import anthropic
import json

client = anthropic.Anthropic()

def stream_json_with_prefill(prompt: str, max_json_tokens: int = 256) -> dict:
    """Stream prefilled JSON response and stop once object is complete."""
    accumulated = "{"
    brace_depth = 1

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_json_tokens,
        messages=[
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "{"}
        ]
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            for ch in text:
                if ch == '{':
                    brace_depth += 1
                elif ch == '}':
                    brace_depth -= 1
                    if brace_depth == 0:
                        # JSON object complete — no need to wait for max_tokens
                        break
            if brace_depth == 0:
                break

    try:
        return json.loads(accumulated)
    except json.JSONDecodeError:
        # Attempt repair
        if not accumulated.endswith("}"):
            accumulated += "}"
        return json.loads(accumulated)

prompts = [
    'Extract the main topic, 3 key points as a list, and a one-sentence summary from: "Machine learning models require large datasets to generalize well. Overfitting occurs when models memorize training data. Regularization techniques like dropout help prevent this."',
    'Parse this address into components (street, city, state, zip): "742 Evergreen Terrace, Springfield, IL 62701"'
]

for prompt in prompts:
    result = stream_json_with_prefill(prompt)
    print(f"Result: {json.dumps(result, indent=2)}\n")

# Expected Token Savings: early termination saves 20-80 tokens on short JSON; streaming avoids wait
# Environment: real-time APIs, interactive extraction, latency-sensitive pipelines
```

### Option 5: Multi-Field Sequential Prefill for Complex Schemas

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def extract_report_with_chained_prefill(document: str) -> dict:
    """
    Chain multiple prefilled requests for complex multi-section extraction.
    Each call is prefilled to its specific field, reducing per-call token waste.
    """
    result = {}

    # Extract title with prefill
    r1 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {"role": "user", "content": f"Extract just the document title from:\n{document[:500]}"},
            {"role": "assistant", "content": '{"title": "'}
        ]
    )
    title_raw = r1.content[0].text
    result["title"] = title_raw.split('"')[0] if '"' in title_raw else title_raw.strip()

    # Extract key findings as array with prefill
    r2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {"role": "user", "content": f"List the 3 main findings as a JSON array of strings from:\n{document}"},
            {"role": "assistant", "content": '["'}
        ]
    )
    findings_raw = '["' + r2.content[0].text
    try:
        result["findings"] = json.loads(findings_raw)
    except Exception:
        result["findings"] = [f.strip() for f in findings_raw.strip('[]"').split('","') if f.strip()]

    # Extract recommendation with prefill
    r3 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[
            {"role": "user", "content": f"What is the primary recommendation from:\n{document}"},
            {"role": "assistant", "content": "The primary recommendation is to "}
        ]
    )
    result["recommendation"] = "To " + r3.content[0].text.strip()

    return result

sample_doc = """
Q3 Performance Review: Cloud Infrastructure Migration

Executive Summary:
Our migration to cloud infrastructure reduced operational costs by 34% while improving uptime to 99.97%.
Key findings show that serverless adoption drove the biggest gains. Container orchestration reduced deployment time.
Legacy systems still account for 40% of incident reports. We recommend accelerating the decommission of on-premise
data centers and investing in developer training for cloud-native patterns.
"""

report = extract_report_with_chained_prefill(sample_doc)
print(json.dumps(report, indent=2))

# Expected Token Savings: ~35% vs single large extraction call; each sub-call is tightly scoped
# Environment: document processing pipelines, report extraction, multi-section form parsing
```

### Option 6: Format-Specific Prefill Library with Reusable Templates

```python
import anthropic
import json
import re
from enum import Enum
from dataclasses import dataclass

client = anthropic.Anthropic()

class OutputFormat(Enum):
    JSON_OBJECT = "json_object"
    JSON_ARRAY = "json_array"
    NUMBERED_LIST = "numbered_list"
    BOOLEAN = "boolean"
    SCORE_1_10 = "score_1_10"
    YES_NO_REASON = "yes_no_reason"

@dataclass
class PrefillTemplate:
    prefill: str
    postprocess: callable
    system_hint: str

PREFILL_LIBRARY: dict[OutputFormat, PrefillTemplate] = {
    OutputFormat.JSON_OBJECT: PrefillTemplate(
        prefill="{",
        postprocess=lambda r: json.loads("{" + r),
        system_hint="Respond only with valid JSON object."
    ),
    OutputFormat.JSON_ARRAY: PrefillTemplate(
        prefill="[",
        postprocess=lambda r: json.loads("[" + r),
        system_hint="Respond only with a valid JSON array."
    ),
    OutputFormat.NUMBERED_LIST: PrefillTemplate(
        prefill="1.",
        postprocess=lambda r: [re.sub(r'^\d+\.\s*', '', line).strip()
                                for line in ("1." + r).split('\n') if re.match(r'^\d+\.', line.strip())],
        system_hint="Respond with a numbered list only. No introduction."
    ),
    OutputFormat.BOOLEAN: PrefillTemplate(
        prefill="",
        postprocess=lambda r: r.strip().lower().startswith(("true", "yes", "1")),
        system_hint="Respond with only 'true' or 'false'."
    ),
    OutputFormat.SCORE_1_10: PrefillTemplate(
        prefill="",
        postprocess=lambda r: int(re.search(r'\d+', r).group()) if re.search(r'\d+', r) else 5,
        system_hint="Respond with only a single integer from 1 to 10."
    ),
    OutputFormat.YES_NO_REASON: PrefillTemplate(
        prefill='{"answer": "',
        postprocess=lambda r: json.loads('{"answer": "' + r) if r.strip().startswith(("yes", "no")) else {"answer": "unknown", "reason": r},
        system_hint='Respond with JSON: {"answer": "yes|no", "reason": "one sentence"}'
    ),
}

def constrained_generate(prompt: str, fmt: OutputFormat, model: str = "claude-haiku-4-5-20251001") -> any:
    template = PREFILL_LIBRARY[fmt]
    messages = [{"role": "user", "content": prompt}]
    if template.prefill:
        messages.append({"role": "assistant", "content": template.prefill})

    resp = client.messages.create(
        model=model,
        max_tokens=256,
        system=template.system_hint,
        messages=messages
    )
    raw = resp.content[0].text
    try:
        return template.postprocess(raw)
    except Exception as e:
        return {"error": str(e), "raw": raw}

# Demo all format templates
examples = [
    ("Extract name, email, company from: 'John Smith, john@acme.com, Acme Corp'", OutputFormat.JSON_OBJECT),
    ("List 4 Python web frameworks", OutputFormat.JSON_ARRAY),
    ("List 3 benefits of test-driven development", OutputFormat.NUMBERED_LIST),
    ("Is Python a statically typed language?", OutputFormat.BOOLEAN),
    ("Rate Python's suitability for data science from 1-10", OutputFormat.SCORE_1_10),
    ("Should a startup use microservices from day one?", OutputFormat.YES_NO_REASON),
]

for prompt, fmt in examples:
    result = constrained_generate(prompt, fmt)
    print(f"[{fmt.value}] {prompt[:50]}")
    print(f"  -> {result}\n")

# Expected Token Savings: 15-45 tokens per call depending on format; eliminates all preamble
# Environment: any structured inference endpoint, multi-format APIs, production extraction pipelines
```

## Comparison

| Option | Format | Savings | Complexity |
|--------|--------|---------|------------|
| 1 | JSON object | ~30 tokens | Low |
| 2 | Code blocks | ~20 tokens | Low |
| 3 | Schema-locked fields | ~25% | Low |
| 4 | Streaming + early stop | 20-80 tokens | Medium |
| 5 | Chained multi-field | ~35% | Medium |
| 6 | Reusable template library | 15-45 tokens | Low (reusable) |
