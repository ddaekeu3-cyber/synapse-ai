---
layout: solution
title: "Agent doesn't use assistant prefill for format control"
category: prompt-engineering
description: "Agent relies on system prompt instructions to enforce output format (JSON, XML, CSV) but the model occasionally ignores them, adds prose wrapping, or uses a different structure. Assistant prefill forces the desired format from the first token."
tags: [prompt-engineering, prefill, format-control, json, structured-output, assistant-turn]
---

## Symptom

The system prompt says "Respond with only a JSON object" but the model occasionally returns "Here is the JSON object:" followed by the JSON, or wraps the output in markdown fences, or adds a trailing explanation. Downstream JSON parsers fail intermittently and the agent needs fallback logic to strip the prose.

## Root Cause

System prompt instructions are guidance, not hard constraints. The model has strong priors toward prose-first responses from RLHF training. When format instructions conflict with those priors, the model occasionally breaks format — especially on edge cases, long conversations, or complex requests.

Assistant prefill solves this by placing the first tokens of the assistant turn yourself. The model continues from where you left it, not from its default response style. Starting with `{` means the first token of the response is guaranteed to be `{`.

## Fix

Add an assistant turn with the format opener as the last message before calling the API. The model will continue generating from that starting point, producing the correct format from token 1.

---

### Option 1 — JSON prefill: start with `{` to force a JSON object

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_json_prefill(user_message: str) -> dict:
    """
    Prefill the assistant turn with '{' to guarantee JSON output.
    The model continues generating from that opening brace.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Extract structured data from the user's message.",
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": "{"},   # ← prefill
        ],
    )
    # The response continues from '{' — prepend it back
    raw = "{" + response.content[0].text
    return json.loads(raw)


# Without prefill: might return "Here is the extracted data:\n```json\n{...}\n```"
# With prefill: returns "{...}" starting from token 1
result = run_with_json_prefill(
    "My name is Alice, I'm 30 years old, and I live in Paris."
)
print(result)
# → {"name": "Alice", "age": 30, "city": "Paris"}
```

**Expected Token Savings:** Eliminates prose preambles (~10–50 tokens); prevents retry turns needed to fix malformed output (~500–1000 tokens each).
**Environment:** Any structured data extraction task; the single most reliable technique for enforcing JSON output without a dedicated structured-output API.

---

### Option 2 — XML prefill for multi-field structured outputs

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_xml_prefill(user_message: str, root_tag: str = "response") -> dict:
    """
    Prefill with the XML root tag opener. Parse the closing tag from the response.
    """
    opener = f"<{root_tag}>"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Respond with a {root_tag} XML element containing: "
            f"<summary>, <sentiment>, and <key_points> (as repeated <point> elements)."
        ),
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": opener},   # ← prefill
        ],
    )
    full_xml = opener + response.content[0].text

    # Parse the fields
    def extract(tag: str, text: str) -> str:
        m = re.search(f"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    points = re.findall(r"<point>(.*?)</point>", full_xml, re.DOTALL)
    return {
        "summary": extract("summary", full_xml),
        "sentiment": extract("sentiment", full_xml),
        "key_points": [p.strip() for p in points],
    }


result = run_with_xml_prefill(
    "The new product launch exceeded expectations with 40% above target sales, "
    "though delivery delays caused some customer frustration."
)
print(result)
```

**Expected Token Savings:** XML prefill guarantees the root tag is present; eliminates cases where the model starts with "Here is the analysis:" before the XML.
**Environment:** Agents using XML as the structured output format; XML prefill is more robust than JSON prefill for multi-section responses.

---

### Option 3 — CSV prefill for tabular data extraction

```python
import anthropic
import csv
import io

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_csv_prefill(user_message: str, headers: list[str]) -> list[dict]:
    """
    Prefill with the CSV header row to guarantee tabular output.
    """
    header_row = ",".join(headers) + "\n"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Extract data as CSV with exactly these columns: {', '.join(headers)}. "
            "No extra columns, no explanatory text, just the data rows."
        ),
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": header_row},  # ← prefill with header
        ],
    )
    full_csv = header_row + response.content[0].text.strip()
    reader = csv.DictReader(io.StringIO(full_csv))
    return list(reader)


result = run_with_csv_prefill(
    "Here are our employees: Alice (Engineering, $120k), Bob (Sales, $85k), Carol (HR, $75k)",
    headers=["name", "department", "salary"],
)
for row in result:
    print(row)
# → [{'name': 'Alice', 'department': 'Engineering', 'salary': '$120k'}, ...]
```

**Expected Token Savings:** Prefilling the header row guarantees column names match exactly; eliminates header case mismatches and extra column hallucinations.
**Environment:** Data extraction agents outputting tabular data; the prefilled header serves as a schema constraint.

---

### Option 4 — Code block prefill: force fenced code output

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_code_prefill(user_message: str, language: str = "python") -> str:
    """
    Prefill with the opening code fence to guarantee a code-only response.
    """
    fence_open = f"```{language}\n"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="Write only the code. No explanation before or after the code block.",
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": fence_open},  # ← prefill
        ],
    )
    full_response = fence_open + response.content[0].text

    # Extract code from the fence
    match = re.search(rf"```{language}\n(.*?)```", full_response, re.DOTALL)
    if match:
        return match.group(1).rstrip()

    # Fallback: strip the fence manually
    code = full_response.replace(fence_open, "").replace("```", "").strip()
    return code


code = run_with_code_prefill(
    "Write a function that checks if a string is a palindrome",
    language="python",
)
print(code)
```

**Expected Token Savings:** Guarantees the model starts with code immediately; prevents "Sure! Here's a Python function..." preambles that add tokens and complicate extraction.
**Environment:** Code generation agents; especially valuable when the output is fed directly to an executor without human review.

---

### Option 5 — Prefill with schema skeleton for complex nested JSON

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def run_with_schema_prefill(user_message: str, schema_skeleton: str) -> dict:
    """
    Prefill with a partial JSON skeleton to guide both structure AND types.
    The model fills in the values starting from the first incomplete field.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "Complete the JSON object below by filling in the values. "
            "Do not change the keys or structure. Output only the completed JSON."
        ),
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": schema_skeleton},  # ← skeleton prefill
        ],
    )
    # Reconstruct full JSON (skeleton + model continuation)
    full = schema_skeleton + response.content[0].text
    # Find the outermost complete JSON object
    depth = 0
    for i, char in enumerate(full):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(full[:i + 1])
    return json.loads(full)


# Prefill with a skeleton that constrains both structure and field names
skeleton = '{"product": {"name": "'

result = run_with_schema_prefill(
    "Product: iPhone 16 Pro, Price: $999, Category: Smartphone, In stock: yes",
    skeleton,
)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Schema skeleton eliminates the model needing to "decide" the structure, reducing output tokens and preventing structural drift.
**Environment:** Complex nested JSON extraction; the skeleton approach is stronger than a system prompt instruction because it pre-commits the structure.

---

### Option 6 — Streaming with prefill for real-time structured output

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def stream_with_json_prefill(user_message: str) -> dict:
    """
    Combine prefill with streaming for real-time structured output.
    Useful for large JSON responses where you want to start processing early.
    """
    accumulated = "{"
    print("{", end="", flush=True)

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Extract all entities as a JSON object with arrays for each type.",
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": "{"},  # ← prefill
        ],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            accumulated += text

    print()  # newline after stream

    # Parse the accumulated JSON
    try:
        return json.loads(accumulated)
    except json.JSONDecodeError:
        # Try to find the last complete JSON
        for end in range(len(accumulated), 0, -1):
            try:
                return json.loads(accumulated[:end])
            except json.JSONDecodeError:
                continue
        return {}


result = stream_with_json_prefill(
    "Text: 'Apple CEO Tim Cook met with Microsoft President Brad Smith in Seattle on Monday.'"
)
print("\nParsed:", result)


# Comparison table
# | Option | Prefill Content | Output Format | Key Benefit |
# |--------|----------------|---------------|-------------|
# | 1 { brace | Single { | JSON object | Most common; guarantee JSON start |
# | 2 <root> tag | XML opener | XML | Multi-section structured XML |
# | 3 CSV header | Header row | CSV | Column schema enforcement |
# | 4 ``` fence | Code fence | Code block | Pure code with no prose |
# | 5 Schema skeleton | Partial JSON | Nested JSON | Structure + type constraints |
# | 6 Streaming + { | { with stream | JSON stream | Real-time large JSON |
```

**Expected Token Savings:** Streaming with prefill starts delivering tokens immediately; for large JSON responses, the first tokens arrive before the full response is generated, enabling pipelined downstream processing.
**Environment:** Production agents generating large structured outputs; the stream+prefill combination gives both format guarantee and low time-to-first-token.
