---
layout: solution
title: "Agent Doesn't Implement Output Format Negotiation with Downstream Consumers"
category: prompt-engineering
description: "Agent always returns output in one fixed format, breaking downstream consumers that expect JSON, markdown, plain text, or structured schemas depending on who's calling."
tags: [prompt-engineering, output-format, api-design, structured-output, json]
---

# Agent Doesn't Implement Output Format Negotiation with Downstream Consumers

## Problem

Agents deployed in diverse pipelines get called by a web frontend (wants markdown), a data pipeline (wants JSON), a voice interface (wants plain sentences), and a reporting system (wants a specific schema). Hardcoding one output format means at least three consumers must post-process the response — adding latency, introducing bugs, and making the agent harder to reuse. Output format negotiation lets callers declare what they need and the agent produces it directly.

## Solution Options

### Option 1: Accept Header-Style Format Selection

```python
import anthropic
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

client = anthropic.Anthropic()

class OutputFormat(Enum):
    MARKDOWN = "markdown"
    JSON = "json"
    PLAIN_TEXT = "plain_text"
    HTML = "html"
    CSV = "csv"

FORMAT_INSTRUCTIONS = {
    OutputFormat.MARKDOWN: (
        "Format your response using Markdown. Use headers (##), bullet lists (-), "
        "bold (**text**), and code blocks (```) where appropriate."
    ),
    OutputFormat.JSON: (
        "Respond with a single valid JSON object only. No prose, no markdown, no explanation. "
        "The JSON must be parseable by Python's json.loads()."
    ),
    OutputFormat.PLAIN_TEXT: (
        "Respond in plain text only. No markdown formatting, no bullet symbols, "
        "no code blocks. Use natural prose sentences."
    ),
    OutputFormat.HTML: (
        "Respond with valid HTML fragment only. Use appropriate tags: "
        "<h2>, <ul>/<li>, <p>, <code>, <strong>. No <html>/<body> wrapper."
    ),
    OutputFormat.CSV: (
        "Respond with CSV data only. First row is the header. "
        "Separate columns with commas. Each row on its own line."
    ),
}

def negotiated_response(
    user_message: str,
    output_format: OutputFormat,
    system_base: str = "You are a helpful assistant.",
    schema: dict | None = None,
) -> Any:
    format_instruction = FORMAT_INSTRUCTIONS[output_format]

    # For JSON format with a schema, include the schema
    if output_format == OutputFormat.JSON and schema:
        format_instruction += f"\n\nRequired JSON schema:\n{json.dumps(schema, indent=2)}"

    system = f"{system_base}\n\n## Output Format\n{format_instruction}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # Auto-parse JSON responses
    if output_format == OutputFormat.JSON:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())

    return raw

# Demonstrate format negotiation
topic = "List the top 3 benefits of using async Python"

print("=== MARKDOWN ===")
print(negotiated_response(topic, OutputFormat.MARKDOWN))

print("\n=== JSON ===")
result = negotiated_response(
    topic,
    OutputFormat.JSON,
    schema={"benefits": [{"title": "string", "description": "string"}]},
)
print(json.dumps(result, indent=2))

print("\n=== PLAIN TEXT ===")
print(negotiated_response(topic, OutputFormat.PLAIN_TEXT))

print("\n=== CSV ===")
print(negotiated_response("List the top 3 Python web frameworks with their pros and cons", OutputFormat.CSV))

# Expected Token Savings: ~10-20% vs post-processing by eliminating conversion steps
# Environment: Multi-consumer APIs where different clients need different formats
```

### Option 2: Schema-Driven JSON Output Negotiation

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

@dataclass
class OutputSchema:
    name: str
    description: str
    json_schema: dict
    example: dict | None = None

# Registry of available output schemas
SCHEMA_REGISTRY: dict[str, OutputSchema] = {
    "summary": OutputSchema(
        name="summary",
        description="Concise summary with key points",
        json_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "key_points": {"type": "array", "items": {"type": "string"}},
                "word_count": {"type": "integer"},
            },
            "required": ["title", "summary", "key_points"],
        },
        example={"title": "Topic", "summary": "Brief summary...", "key_points": ["Point 1", "Point 2"]},
    ),
    "comparison": OutputSchema(
        name="comparison",
        description="Side-by-side comparison of options",
        json_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "pros": {"type": "array", "items": {"type": "string"}},
                            "cons": {"type": "array", "items": {"type": "string"}},
                            "score": {"type": "number"},
                        },
                        "required": ["name", "pros", "cons"],
                    },
                },
                "recommendation": {"type": "string"},
            },
            "required": ["topic", "options"],
        },
    ),
    "action_items": OutputSchema(
        name="action_items",
        description="Actionable task list",
        json_schema={
            "type": "object",
            "properties": {
                "context": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                            "owner": {"type": "string"},
                        },
                        "required": ["task", "priority"],
                    },
                },
            },
            "required": ["items"],
        },
    ),
}

def schema_negotiated_call(user_message: str, schema_name: str) -> dict:
    """Call the agent with a specific output schema contract."""
    if schema_name not in SCHEMA_REGISTRY:
        raise ValueError(f"Unknown schema: {schema_name}. Available: {list(SCHEMA_REGISTRY.keys())}")

    schema = SCHEMA_REGISTRY[schema_name]
    example_str = f"\n\nExample output:\n{json.dumps(schema.example, indent=2)}" if schema.example else ""

    system = (
        f"You are a helpful assistant. Always respond with a single valid JSON object "
        f"matching this exact schema:\n\n{json.dumps(schema.json_schema, indent=2)}"
        f"{example_str}\n\nReturn ONLY valid JSON. No prose, no markdown, no code fences."
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    # Strip code fences if model adds them
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)

# Different consumers request different schemas
print("=== Summary schema ===")
result = schema_negotiated_call(
    "Explain the benefits of microservices architecture",
    "summary",
)
print(json.dumps(result, indent=2))

print("\n=== Comparison schema ===")
result = schema_negotiated_call(
    "Compare PostgreSQL vs MongoDB for a social media application",
    "comparison",
)
print(json.dumps(result, indent=2))

print("\n=== Action Items schema ===")
result = schema_negotiated_call(
    "I need to migrate my app to Kubernetes. What should I do?",
    "action_items",
)
print(json.dumps(result, indent=2))

# Expected Token Savings: 15-30% vs having consumers reformat and re-parse output
# Environment: Data pipelines, reporting systems, and APIs with strict output contracts
```

### Option 3: Audience-Aware Format Negotiation

```python
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class Audience(Enum):
    DEVELOPER = "developer"
    EXECUTIVE = "executive"
    BEGINNER = "beginner"
    VOICE_ASSISTANT = "voice_assistant"
    DATA_ANALYST = "data_analyst"

AUDIENCE_PROFILES = {
    Audience.DEVELOPER: {
        "format": "markdown",
        "instruction": (
            "The audience is a software engineer. Use technical terminology freely. "
            "Include code examples in appropriate language. Use markdown with headers, "
            "code blocks, and bullet lists. Be precise and detailed."
        ),
        "max_tokens": 800,
    },
    Audience.EXECUTIVE: {
        "format": "prose",
        "instruction": (
            "The audience is a non-technical executive. Avoid jargon. "
            "Focus on business impact, ROI, and risk. Use short paragraphs, "
            "no code, no technical acronyms without explanation. "
            "Lead with the key takeaway. Maximum 3 paragraphs."
        ),
        "max_tokens": 400,
    },
    Audience.BEGINNER: {
        "format": "prose",
        "instruction": (
            "The audience is a complete beginner. Use simple language a 12-year-old "
            "could understand. Use analogies to everyday objects. Short sentences. "
            "Define every technical term you use. Be encouraging."
        ),
        "max_tokens": 500,
    },
    Audience.VOICE_ASSISTANT: {
        "format": "plain_text",
        "instruction": (
            "This response will be read aloud by a text-to-speech system. "
            "Use only plain sentences. No bullet points, no markdown, no symbols, "
            "no URLs. Write naturally as you would speak. "
            "Keep each sentence under 20 words. No more than 4 sentences total."
        ),
        "max_tokens": 200,
    },
    Audience.DATA_ANALYST: {
        "format": "structured",
        "instruction": (
            "The audience is a data analyst. Provide quantitative information where possible. "
            "Use tables (markdown format) for comparisons. Include numeric ranges, "
            "percentages, and statistical terms where appropriate. "
            "Prefer structured lists over prose."
        ),
        "max_tokens": 600,
    },
}

@dataclass
class NegotiatedResponse:
    content: str
    format: str
    audience: str
    token_count: int

def audience_aware_response(
    user_message: str,
    audience: Audience,
    base_system: str = "You are a helpful assistant.",
) -> NegotiatedResponse:
    profile = AUDIENCE_PROFILES[audience]

    system = f"{base_system}\n\n## Communication Style\n{profile['instruction']}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=profile["max_tokens"],
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    return NegotiatedResponse(
        content=response.content[0].text,
        format=profile["format"],
        audience=audience.value,
        token_count=response.usage.output_tokens,
    )

question = "What is Kubernetes and why would I use it?"

for audience in Audience:
    result = audience_aware_response(question, audience)
    print(f"\n=== {audience.value.upper()} (format={result.format}, tokens={result.token_count}) ===")
    print(result.content[:300] + ("..." if len(result.content) > 300 else ""))

# Expected Token Savings: 20-40% for voice/executive audiences vs developer-style verbose output
# Environment: Multi-channel AI products (web app, voice, dashboard, API) from a single agent
```

### Option 4: Content-Type Header Negotiation via Tool Use

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

client = anthropic.Anthropic()

@dataclass
class FormatRequest:
    """Caller declares what they want; agent negotiates."""
    accept: list[str]          # Preferred formats in priority order
    schema: dict | None        # Required JSON schema if JSON is accepted
    max_length: int | None     # Optional length constraint
    locale: str = "en"         # Language/locale

def format_negotiation_system(request: FormatRequest) -> str:
    formats_str = ", ".join(request.accept)
    system = f"""You are a helpful assistant that adapts output format to caller requirements.

## Format Negotiation
Accepted formats (in priority order): {formats_str}

Choose the first format from the list that suits the content.

Format rules:
- "application/json": Return ONLY valid JSON. No prose.
- "text/markdown": Use Markdown formatting with headers and lists.
- "text/plain": Plain prose, no markdown symbols.
- "text/html": HTML fragment with semantic tags.

"""
    if request.schema and "application/json" in request.accept:
        system += f"## JSON Schema (when using application/json)\n{json.dumps(request.schema, indent=2)}\n\n"

    if request.max_length:
        system += f"## Length Constraint\nKeep response under {request.max_length} characters.\n\n"

    system += f"## Locale\nRespond in locale: {request.locale}"
    return system

def negotiated_call(user_message: str, request: FormatRequest) -> tuple[str, Any]:
    """Returns (chosen_format, parsed_content)."""
    system = format_negotiation_system(request)

    # Use prefill to force format selection
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # Detect and parse the chosen format
    if raw.startswith("{") or raw.startswith("["):
        try:
            return "application/json", json.loads(raw)
        except json.JSONDecodeError:
            pass

    if raw.startswith("<"):
        return "text/html", raw

    if any(md in raw for md in ["##", "**", "- ", "```"]):
        return "text/markdown", raw

    return "text/plain", raw

# Simulate different consumers with different format preferences

# Consumer 1: REST API client that wants JSON with a schema
api_request = FormatRequest(
    accept=["application/json", "text/plain"],
    schema={"answer": "string", "confidence": "number (0-1)", "sources": ["string"]},
    max_length=500,
)
fmt, content = negotiated_call("What are the main causes of technical debt?", api_request)
print(f"[API Client] Got: {fmt}")
if isinstance(content, dict):
    print(json.dumps(content, indent=2))
else:
    print(content[:200])

# Consumer 2: Docs site that wants Markdown
docs_request = FormatRequest(
    accept=["text/markdown", "text/html", "text/plain"],
    schema=None,
    max_length=800,
)
fmt, content = negotiated_call("Explain Docker containers for developers", docs_request)
print(f"\n[Docs Site] Got: {fmt}")
print(str(content)[:300])

# Consumer 3: Voice app that only accepts plain text
voice_request = FormatRequest(
    accept=["text/plain"],
    schema=None,
    max_length=200,
)
fmt, content = negotiated_call("What time is it best to exercise?", voice_request)
print(f"\n[Voice App] Got: {fmt}")
print(content)

# Expected Token Savings: 10-25% by eliminating post-processing and re-formatting steps
# Environment: REST APIs with Content-Type negotiation, multi-channel platforms
```

### Option 5: Streaming Format Negotiation with Early Termination

```python
import anthropic
import json
import re
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class StreamFormat(Enum):
    JSON_STREAM = "json_stream"      # Stream, then parse at end
    MARKDOWN_STREAM = "markdown"     # Stream directly to frontend
    STRUCTURED = "structured"        # Stream with section markers

@dataclass
class StreamFormatConfig:
    format: StreamFormat
    system_suffix: str
    post_process: bool  # Whether to parse after streaming

STREAM_CONFIGS = {
    StreamFormat.JSON_STREAM: StreamFormatConfig(
        format=StreamFormat.JSON_STREAM,
        system_suffix="Respond with a single valid JSON object only. No other text.",
        post_process=True,
    ),
    StreamFormat.MARKDOWN_STREAM: StreamFormatConfig(
        format=StreamFormat.MARKDOWN_STREAM,
        system_suffix="Format your response in Markdown with appropriate headers and lists.",
        post_process=False,
    ),
    StreamFormat.STRUCTURED: StreamFormatConfig(
        format=StreamFormat.STRUCTURED,
        system_suffix=(
            "Structure your response with XML-like section markers:\n"
            "<summary>One sentence</summary>\n"
            "<details>Full explanation</details>\n"
            "<action>Recommended next step</action>"
        ),
        post_process=True,
    ),
}

def stream_with_format_negotiation(
    user_message: str,
    stream_format: StreamFormat,
    base_system: str = "You are a helpful assistant.",
) -> dict | str:
    config = STREAM_CONFIGS[stream_format]
    system = f"{base_system}\n\n{config.system_suffix}"

    full_text = ""
    print(f"[STREAM:{stream_format.value}] ", end="", flush=True)

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_text += text

    print()  # newline after stream

    if not config.post_process:
        return full_text

    # Post-process structured formats
    if stream_format == StreamFormat.JSON_STREAM:
        clean = full_text.strip()
        if "```" in clean:
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return {"raw": full_text, "parse_error": True}

    if stream_format == StreamFormat.STRUCTURED:
        result = {}
        for tag in ["summary", "details", "action"]:
            match = re.search(rf"<{tag}>(.*?)</{tag}>", full_text, re.DOTALL)
            if match:
                result[tag] = match.group(1).strip()
        return result

    return full_text

topic = "What is the best way to handle database migrations in production?"

print("=== JSON Stream ===")
result = stream_with_format_negotiation(
    topic, StreamFormat.JSON_STREAM,
    base_system="You are a database expert. Return a JSON with: approach (string), steps (array), risks (array).",
)
if isinstance(result, dict):
    print(f"\nParsed: {json.dumps(result, indent=2)}")

print("\n=== Markdown Stream ===")
result = stream_with_format_negotiation(topic, StreamFormat.MARKDOWN_STREAM)
print(f"\n(Rendered directly to frontend, {len(str(result))} chars)")

print("\n=== Structured Sections ===")
result = stream_with_format_negotiation(topic, StreamFormat.STRUCTURED)
if isinstance(result, dict):
    for k, v in result.items():
        print(f"\n[{k.upper()}] {v[:100]}...")

# Expected Token Savings: Streaming + format negotiation avoids full round-trip for transformations
# Environment: Web frontends that stream markdown, APIs that need JSON, dashboards needing sections
```

### Option 6: Versioned Output Schema with Migration Support

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any, Callable

client = anthropic.Anthropic()

@dataclass
class SchemaVersion:
    version: str
    system_instructions: str
    migrate_from: dict[str, Callable] = field(default_factory=dict)  # {from_version: migration_fn}

# Define versioned schemas with migration paths
SCHEMA_VERSIONS: dict[str, SchemaVersion] = {
    "v1": SchemaVersion(
        version="v1",
        system_instructions=(
            "Respond with JSON: {\"result\": string, \"tags\": [string]}"
        ),
    ),
    "v2": SchemaVersion(
        version="v2",
        system_instructions=(
            "Respond with JSON: {\"result\": string, \"tags\": [string], "
            "\"confidence\": number (0-1), \"sources\": [string]}"
        ),
        migrate_from={
            "v1": lambda v1: {**v1, "confidence": 0.8, "sources": []},  # Upgrade v1 → v2
        },
    ),
    "v3": SchemaVersion(
        version="v3",
        system_instructions=(
            "Respond with JSON: {\"answer\": {\"text\": string, \"confidence\": number}, "
            "\"metadata\": {\"tags\": [string], \"sources\": [string], \"model_version\": string}}"
        ),
        migrate_from={
            "v2": lambda v2: {
                "answer": {"text": v2["result"], "confidence": v2.get("confidence", 0.8)},
                "metadata": {"tags": v2.get("tags", []), "sources": v2.get("sources", []), "model_version": "unknown"},
            },
            "v1": lambda v1: {
                "answer": {"text": v1["result"], "confidence": 0.8},
                "metadata": {"tags": v1.get("tags", []), "sources": [], "model_version": "unknown"},
            },
        },
    ),
}

def versioned_call(
    user_message: str,
    requested_version: str,
    base_system: str = "You are a helpful assistant.",
) -> dict:
    """Return response in exactly the requested schema version."""
    if requested_version not in SCHEMA_VERSIONS:
        raise ValueError(f"Unknown schema version: {requested_version}. Available: {list(SCHEMA_VERSIONS.keys())}")

    target_schema = SCHEMA_VERSIONS[requested_version]

    # Try to generate directly in requested version
    system = f"{base_system}\n\n## Output Schema ({requested_version})\n{target_schema.system_instructions}\n\nReturn ONLY valid JSON."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    result = json.loads(raw)
    result["_schema_version"] = requested_version
    return result

def migrate_response(response: dict, target_version: str) -> dict:
    """Migrate a response from one schema version to another."""
    current_version = response.get("_schema_version", "v1")
    if current_version == target_version:
        return response

    target = SCHEMA_VERSIONS.get(target_version)
    if not target:
        raise ValueError(f"Unknown target version: {target_version}")

    migrator = target.migrate_from.get(current_version)
    if not migrator:
        raise ValueError(f"No migration path from {current_version} to {target_version}")

    migrated = migrator({k: v for k, v in response.items() if k != "_schema_version"})
    migrated["_schema_version"] = target_version
    migrated["_migrated_from"] = current_version
    return migrated

question = "What is event sourcing in software architecture?"

# Different consumers request different schema versions
print("=== Consumer A: requests v1 (legacy system) ===")
v1_response = versioned_call(question, "v1")
print(json.dumps(v1_response, indent=2))

print("\n=== Consumer B: requests v3 (modern system) ===")
v3_response = versioned_call(question, "v3")
print(json.dumps(v3_response, indent=2))

print("\n=== Consumer C: receives v1, needs v3 (migration) ===")
legacy_response = {"result": "Event sourcing stores state as events", "tags": ["architecture", "patterns"], "_schema_version": "v1"}
migrated = migrate_response(legacy_response, "v3")
print(json.dumps(migrated, indent=2))

# Expected Token Savings: Avoids redundant re-calls when consumers need format updates; migrate instead
# Environment: Long-lived APIs with multiple consumer versions; enables zero-downtime schema evolution
```

## Comparison

| Option | Negotiation Mechanism | Consumer Control | Streaming | Schema Versioning | Best For |
|--------|----------------------|-----------------|-----------|------------------|---------|
| 1. Enum Format Selection | Output format enum | Caller picks format | No | No | Basic multi-format APIs |
| 2. Schema-Driven JSON | Named JSON schemas | Caller picks schema | No | No | Data pipelines with strict contracts |
| 3. Audience-Aware | Audience persona | Caller picks audience | No | No | Multi-channel products (web, voice, exec) |
| 4. Content-Type Header | MIME types + priority | Caller declares preferences | No | No | REST APIs with Accept header patterns |
| 5. Streaming Format | Stream + format config | Caller picks stream type | Yes | No | Web frontends and real-time dashboards |
| 6. Versioned Schema | Schema version string | Caller picks version | No | Yes | Long-lived APIs with multiple consumer versions |
