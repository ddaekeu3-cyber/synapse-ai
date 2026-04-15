---
layout: solution
title: "Agent Doesn't Implement Structured Streaming Output"
category: streaming
description: "Agents that stream unstructured text force clients to wait for the full response before parsing JSON or structured data. These patterns stream structured outputs incrementally so clients can process fields as they arrive."
tags: [streaming, structured-output, json, sse, incremental-parsing, real-time]
---

# Agent Doesn't Implement Structured Streaming Output

## The Problem

When an agent needs to return structured data (JSON reports, multi-field extractions, step-by-step plans), most implementations generate the full response and then parse it. This forces clients to wait for 100% completion before acting on any field. A report with 10 sections makes the user wait for all 10 before seeing section 1.

Structured streaming lets clients react to fields progressively: display section 1 while 2–10 are still generating, trigger downstream actions on the first completed field, or abandon the stream early if the first field fails validation.

---

## Option 1: JSON Field Streaming with Prefix Detection

Stream a JSON object and emit events each time a top-level field completes.

```python
import anthropic
import json
import re
from collections.abc import Generator

client = anthropic.Anthropic()

def stream_json_fields(
    prompt: str,
    expected_fields: list[str],
    system_prompt: str = ""
) -> Generator[dict, None, None]:
    """
    Stream a JSON response and yield each top-level field as it completes.
    Yields: {"event": "field_complete", "field": "name", "value": ...}
    """
    accumulated = ""
    yielded_fields: set[str] = set()

    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        kwargs = {"system": system_prompt}
    else:
        kwargs = {}

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        for text in stream.text_stream:
            accumulated += text

            # Check if any expected fields have completed
            for field in expected_fields:
                if field in yielded_fields:
                    continue

                # Look for completed field pattern: "field": <value>,
                # or "field": <value>\n} (last field)
                pattern = rf'"{re.escape(field)}":\s*("(?:[^"\\]|\\.)*"|\d+\.?\d*|true|false|null|\[(?:[^\[\]]|\[[^\[\]]*\])*\])'
                match = re.search(pattern, accumulated)
                if match:
                    raw_value = match.group(1)
                    try:
                        value = json.loads(raw_value)
                        yielded_fields.add(field)
                        yield {
                            "event": "field_complete",
                            "field": field,
                            "value": value
                        }
                    except json.JSONDecodeError:
                        pass

            # Emit raw chunk for clients that want streaming text
            yield {"event": "chunk", "text": text}

    # Emit the full parsed JSON at end
    try:
        # Extract JSON from accumulated text
        json_match = re.search(r'\{.*\}', accumulated, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            yield {"event": "complete", "data": parsed}
    except json.JSONDecodeError:
        yield {"event": "error", "message": "Failed to parse final JSON"}

# Usage
prompt = """Analyze this product description and return JSON with fields:
- name (string): product name
- category (string): product category
- price_range (string): estimated price range
- sentiment (string): positive/neutral/negative
- summary (string): one sentence summary

Product: "The AcmePro X500 is a premium wireless noise-canceling headphone with 40-hour battery life, priced at $349."

Return only valid JSON."""

print("Streaming structured analysis:")
for event in stream_json_fields(
    prompt,
    expected_fields=["name", "category", "price_range", "sentiment", "summary"]
):
    if event["event"] == "field_complete":
        print(f"  [FIELD READY] {event['field']}: {event['value']}")
    elif event["event"] == "complete":
        print(f"\nFull result: {json.dumps(event['data'], indent=2)}")

# Expected Token Savings: No extra API calls vs batch; same tokens but clients can act on partial data immediately
# Environment: dashboards displaying partial results, pipelines triggering on first field, streaming APIs
```

---

## Option 2: Newline-Delimited JSON (NDJSON) Streaming

Structure the output as one JSON object per line, emitting each line as it completes.

```python
import anthropic
import json
from collections.abc import Generator

client = anthropic.Anthropic()

NDJSON_SYSTEM = """You are a data extraction agent. Always respond with newline-delimited JSON (NDJSON).
Each line must be a complete, valid JSON object.
Never wrap output in a JSON array. One object per line, one line per item.
Example format:
{"type": "item", "id": 1, "data": "..."}
{"type": "item", "id": 2, "data": "..."}
{"type": "summary", "total": 2}"""

def stream_ndjson(
    prompt: str,
    on_object: callable | None = None
) -> Generator[dict, None, None]:
    """
    Stream NDJSON output, yielding each complete JSON object as it's received.
    Optionally calls on_object(obj) for immediate side effects.
    """
    accumulated_line = ""
    objects_received = 0

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=NDJSON_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            accumulated_line += text

            # Check for complete lines
            while '\n' in accumulated_line:
                line, accumulated_line = accumulated_line.split('\n', 1)
                line = line.strip()

                if not line:
                    continue

                try:
                    obj = json.loads(line)
                    objects_received += 1
                    if on_object:
                        on_object(obj)
                    yield obj
                except json.JSONDecodeError:
                    # Partial line — put back and continue accumulating
                    accumulated_line = line + '\n' + accumulated_line
                    break

        # Handle last line without trailing newline
        if accumulated_line.strip():
            try:
                obj = json.loads(accumulated_line.strip())
                if on_object:
                    on_object(obj)
                yield obj
            except json.JSONDecodeError:
                pass

def process_as_received(obj: dict):
    """Side effect: immediately process each object as it arrives."""
    obj_type = obj.get("type", "unknown")
    if obj_type == "issue":
        print(f"  [ISSUE #{obj.get('id')}] {obj.get('severity', '?')}: {obj.get('description', '')[:60]}")
    elif obj_type == "summary":
        print(f"  [SUMMARY] {obj.get('total_issues')} issues found")

# Usage
prompt = """Analyze this Python code for issues. Return each issue as a separate NDJSON line with:
{"type": "issue", "id": N, "severity": "high/medium/low", "description": "...", "line": N}
Then a final summary line:
{"type": "summary", "total_issues": N}

Code to analyze:
```python
import os
password = "admin123"  # hardcoded password
user_input = input()
query = "SELECT * FROM users WHERE name = '" + user_input + "'"  # SQL injection
eval(user_input)  # dangerous eval
```"""

print("Streaming code analysis (object-by-object):")
all_objects = list(stream_ndjson(prompt, on_object=process_as_received))
print(f"\nTotal objects received: {len(all_objects)}")

# Expected Token Savings: Zero overhead vs standard call; NDJSON enables progressive rendering without extra requests
# Environment: code analysis, batch data extraction, report generation with many items
```

---

## Option 3: Structured Streaming with Prefill

Use assistant prefill to lock in the JSON structure, then stream only the values.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def stream_with_prefill(
    prompt: str,
    json_template: dict,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024
) -> tuple[str, dict | None]:
    """
    Use assistant prefill to anchor the JSON structure.
    Returns (full_text, parsed_json).
    """
    # Build prefill from template keys
    prefill_keys = ', '.join(f'"{k}": ' for k in json_template.keys())
    prefill = f'{{{prefill_keys}'

    accumulated = prefill

    print(f"Prefill anchor: {prefill}")
    print("Streaming values:")

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": prefill}  # Prefill anchors structure
        ]
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            print(text, end='', flush=True)

    print()  # newline after stream

    # Ensure closing brace
    if not accumulated.rstrip().endswith('}'):
        accumulated = accumulated.rstrip() + '}'

    # Parse result
    try:
        # Try direct parse
        parsed = json.loads(accumulated)
    except json.JSONDecodeError:
        # Try extracting JSON block
        json_match = re.search(r'\{.*\}', accumulated, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

    return accumulated, parsed

# Usage
template = {
    "sentiment": None,
    "confidence": None,
    "key_topics": None,
    "action_required": None,
    "priority": None
}

prompt = """Analyze this customer support message and fill in each field.

Message: "I've been waiting 3 weeks for my order #12345 and still haven't received it.
This is completely unacceptable. I need this resolved TODAY or I'm disputing the charge."

Return a JSON object with:
- sentiment: "positive/neutral/negative/urgent"
- confidence: 0.0-1.0
- key_topics: list of main topics
- action_required: true/false
- priority: "low/medium/high/critical"
"""

text, result = stream_with_prefill(prompt, template)
if result:
    print(f"\nParsed result: {json.dumps(result, indent=2)}")
else:
    print(f"\nParse failed. Raw: {text}")

# Expected Token Savings: Prefill eliminates the model needing to generate JSON key names; saves 20-30% of output tokens
# Environment: high-volume classification, sentiment analysis, structured extraction pipelines
```

---

## Option 4: Progressive Section Streaming

For multi-section documents, stream each section as it completes and yield section objects.

```python
import anthropic
import re
from dataclasses import dataclass
from collections.abc import Generator

client = anthropic.Anthropic()

@dataclass
class StreamedSection:
    title: str
    content: str
    index: int
    is_complete: bool = False

def stream_sections(
    prompt: str,
    section_delimiter: str = "##",
    system_prompt: str = ""
) -> Generator[StreamedSection, None, None]:
    """
    Stream a multi-section response, yielding each section as it completes.
    Sections are delimited by markdown headers (## by default).
    """
    accumulated = ""
    current_title = ""
    current_content = ""
    section_index = 0
    in_section = False

    kwargs = {"system": system_prompt} if system_prompt else {}

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
        **kwargs
    ) as stream:
        for text in stream.text_stream:
            accumulated += text

            # Check for section boundary
            lines = accumulated.split('\n')

            for i, line in enumerate(lines[:-1]):  # Process all but last (may be partial)
                stripped = line.strip()

                if stripped.startswith(section_delimiter + ' '):
                    # New section header found
                    if in_section and current_content.strip():
                        # Yield the completed previous section
                        yield StreamedSection(
                            title=current_title,
                            content=current_content.strip(),
                            index=section_index,
                            is_complete=True
                        )
                        section_index += 1

                    current_title = stripped[len(section_delimiter):].strip()
                    current_content = ""
                    in_section = True
                elif in_section:
                    current_content += line + '\n'

            # Keep the last potentially incomplete line for next iteration
            accumulated = lines[-1]

    # Handle remaining content after stream ends
    if accumulated.strip():
        current_content += accumulated

    if in_section and current_content.strip():
        yield StreamedSection(
            title=current_title,
            content=current_content.strip(),
            index=section_index,
            is_complete=True
        )

# Usage
prompt = """Write a technical report on microservices architecture with exactly these sections:
## Overview
Brief overview of microservices (2-3 sentences).
## Benefits
3 key benefits with brief explanations.
## Challenges
3 key challenges with brief explanations.
## Recommendation
One paragraph recommendation.

Use exactly the ## header format shown."""

print("Streaming report sections as they complete:\n")
sections = []
for section in stream_sections(prompt):
    print(f"[SECTION {section.index + 1} COMPLETE] '{section.title}'")
    print(f"  Preview: {section.content[:100]}...")
    print()
    sections.append(section)

print(f"Total sections streamed: {len(sections)}")

# Expected Token Savings: Clients can display/process section 1 while 2-4 still generate; no extra token cost
# Environment: report generation, long-form content, document editors, progressive rendering UIs
```

---

## Option 5: Streaming with Schema Validation on Complete

Stream freely, then validate the full output against a schema once complete — with partial display during streaming.

```python
import anthropic
import json
import re
from typing import Any

client = anthropic.Anthropic()

def validate_against_schema(data: dict, schema: dict) -> list[str]:
    """Simple schema validator. Returns list of validation errors."""
    errors = []

    for field, spec in schema.items():
        if spec.get("required", False) and field not in data:
            errors.append(f"Missing required field: {field}")
            continue

        if field not in data:
            continue

        value = data[field]
        expected_type = spec.get("type")

        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"Field '{field}' should be string, got {type(value).__name__}")
        elif expected_type == "number" and not isinstance(value, (int, float)):
            errors.append(f"Field '{field}' should be number, got {type(value).__name__}")
        elif expected_type == "boolean" and not isinstance(value, bool):
            errors.append(f"Field '{field}' should be boolean, got {type(value).__name__}")
        elif expected_type == "array" and not isinstance(value, list):
            errors.append(f"Field '{field}' should be array, got {type(value).__name__}")

        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"Field '{field}' value '{value}' not in allowed: {spec['enum']}")

        if "min" in spec and isinstance(value, (int, float)) and value < spec["min"]:
            errors.append(f"Field '{field}' value {value} below minimum {spec['min']}")

        if "max" in spec and isinstance(value, (int, float)) and value > spec["max"]:
            errors.append(f"Field '{field}' value {value} above maximum {spec['max']}")

    return errors

def stream_and_validate(
    prompt: str,
    output_schema: dict[str, Any],
    repair_on_error: bool = True
) -> dict:
    """
    Stream response with live display, then validate the full output.
    Optionally repairs schema violations.
    """
    accumulated = ""

    print("Streaming response:")
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            print(text, end='', flush=True)

    print('\n')

    # Extract and parse JSON
    json_match = re.search(r'\{.*\}', accumulated, re.DOTALL)
    parsed = None
    validation_errors = []

    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            validation_errors = validate_against_schema(parsed, output_schema)
        except json.JSONDecodeError as e:
            validation_errors = [f"JSON parse error: {e}"]
    else:
        validation_errors = ["No JSON object found in response"]

    # Repair if needed
    if validation_errors and repair_on_error and parsed is not None:
        print(f"Validation issues: {validation_errors}")
        print("Attempting repair...")

        repair_prompt = f"""The following JSON has schema violations:
{json.dumps(parsed, indent=2)}

Schema requirements:
{json.dumps(output_schema, indent=2)}

Violations:
{chr(10).join(f'- {e}' for e in validation_errors)}

Return a corrected JSON object that satisfies the schema. Return only the JSON."""

        repair_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": repair_prompt}]
        )
        repair_text = repair_resp.content[0].text
        repair_match = re.search(r'\{.*\}', repair_text, re.DOTALL)
        if repair_match:
            try:
                parsed = json.loads(repair_match.group(0))
                validation_errors = validate_against_schema(parsed, output_schema)
            except json.JSONDecodeError:
                pass

    return {
        "parsed": parsed,
        "validation_errors": validation_errors,
        "valid": len(validation_errors) == 0,
        "raw": accumulated
    }

# Usage
schema = {
    "score": {"type": "number", "required": True, "min": 0, "max": 10},
    "grade": {"type": "string", "required": True, "enum": ["A", "B", "C", "D", "F"]},
    "passed": {"type": "boolean", "required": True},
    "feedback": {"type": "string", "required": True},
    "topics_covered": {"type": "array", "required": True}
}

prompt = """Grade this essay response and return a JSON evaluation.

Essay: "Machine learning is when computers learn from data. Neural networks are inspired by the brain."

Return JSON with: score (0-10), grade (A/B/C/D/F), passed (true/false), feedback (string), topics_covered (array of strings)."""

result = stream_and_validate(prompt, schema)
print(f"Valid: {result['valid']}")
if result['validation_errors']:
    print(f"Errors: {result['validation_errors']}")
if result['parsed']:
    print(f"Score: {result['parsed'].get('score')}, Grade: {result['parsed'].get('grade')}")

# Expected Token Savings: Stream first (no extra cost); Haiku repair only fires on violations (~15% of cases)
# Environment: grading systems, form processing, structured extraction with strict schemas
```

---

## Option 6: Server-Sent Events (SSE) Structured Stream Proxy

Proxy Claude streaming to a frontend via SSE, emitting typed structured events.

```python
import anthropic
import json
import re
import asyncio
from collections.abc import AsyncGenerator

client = anthropic.AsyncAnthropic()

async def structured_sse_stream(
    prompt: str,
    field_names: list[str],
    model: str = "claude-sonnet-4-6"
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted events for a structured JSON stream.
    Emits: data_chunk, field_ready, stream_complete, stream_error events.
    """
    accumulated = ""
    emitted_fields: set[str] = set()

    def sse_event(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            async for text in stream.text_stream:
                accumulated += text

                # Emit raw chunk
                yield sse_event("data_chunk", {"text": text})

                # Check for completed fields
                for field in field_names:
                    if field in emitted_fields:
                        continue
                    pattern = rf'"{re.escape(field)}":\s*("(?:[^"\\]|\\.)*"|\d+\.?\d*|true|false)'
                    match = re.search(pattern, accumulated)
                    if match:
                        try:
                            value = json.loads(match.group(1))
                            emitted_fields.add(field)
                            yield sse_event("field_ready", {
                                "field": field,
                                "value": value
                            })
                        except json.JSONDecodeError:
                            pass

        # Parse complete JSON
        json_match = re.search(r'\{.*\}', accumulated, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                yield sse_event("stream_complete", {"data": parsed})
            except json.JSONDecodeError:
                yield sse_event("stream_error", {"message": "JSON parse failed"})
        else:
            yield sse_event("stream_error", {"message": "No JSON in response"})

    except Exception as e:
        yield sse_event("stream_error", {"message": str(e)})

async def demo_sse_stream():
    """Demonstrate SSE structured stream output."""
    prompt = """Classify this support ticket and return JSON:
{"priority": "low/medium/high/critical", "category": "billing/technical/account/other",
 "sentiment": "positive/neutral/negative", "auto_resolve": true/false}

Ticket: "My payment failed but I was still charged twice. This is urgent, please fix ASAP!"
Return only valid JSON."""

    fields = ["priority", "category", "sentiment", "auto_resolve"]
    print("SSE stream output:")
    print("-" * 40)

    async for event_str in structured_sse_stream(prompt, fields):
        # Parse back for demo display
        lines = event_str.strip().split('\n')
        event_type = lines[0].replace('event: ', '')
        data = json.loads(lines[1].replace('data: ', ''))

        if event_type == "field_ready":
            print(f"[FIELD READY] {data['field']} = {data['value']}")
        elif event_type == "stream_complete":
            print(f"\n[COMPLETE] {json.dumps(data['data'], indent=2)}")
        elif event_type == "stream_error":
            print(f"[ERROR] {data['message']}")
        # Skip data_chunk events in demo output

asyncio.run(demo_sse_stream())

# Expected Token Savings: SSE proxy adds zero token overhead; enables real-time frontend updates without polling
# Environment: web frontends, React dashboards, real-time classification UIs, streaming APIs
```

---

## Comparison

| Option | Streaming Approach | Latency to First Field | Parse Strategy | Best For |
|--------|-------------------|----------------------|----------------|----------|
| 1. JSON Field Detection | Regex on accumulating text | ~200ms after field completes | Regex + JSON.parse | Dashboards, pipelines |
| 2. NDJSON Lines | One object per line | Per newline | Line-by-line JSON.parse | Batch items, lists |
| 3. Prefill Anchoring | Prefill locks structure | First value immediately | Parse full response | Classification, extraction |
| 4. Section Streaming | Markdown header detection | Per section | String splitting | Reports, documents |
| 5. Stream + Validate | Full stream, then validate | End of stream | Schema validation | Strict schema requirements |
| 6. SSE Proxy | SSE events to frontend | Per field | Event-driven | Web UIs, frontends |

**Recommended defaults:**
- **Frontend/UI** → Option 6 (SSE proxy)
- **Reports/documents** → Option 4 (section streaming)
- **Batch extraction** → Option 2 (NDJSON)
- **Classification** → Option 3 (prefill) or Option 1 (field detection)
- **Strict schema** → Option 5 (stream + validate)
