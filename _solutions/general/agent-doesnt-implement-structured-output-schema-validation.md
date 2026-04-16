---
layout: solution
title: "Agent Doesn't Implement Structured Output Schema Validation"
category: general
description: "Agents that accept raw LLM text output without schema validation pass malformed, incomplete, or type-mismatched data downstream. Schema validation catches output errors at the boundary, triggers repair prompts, and prevents silent data corruption."
tags: [general, schema-validation, structured-output, json, reliability, python]
---

## Problem

LLMs produce probabilistic output. Even with structured output instructions, models occasionally omit required fields, produce wrong types, or generate invalid JSON. Without schema validation, these errors silently corrupt downstream systems — databases receive null values, APIs crash on unexpected types, and UIs display broken states. Validation at the agent boundary catches these errors immediately.

## Solutions

### Option 1: JSON Schema Validation with Auto-Repair Prompt

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any, Optional

EXTRACT_SCHEMA = {
    "type": "object",
    "required": ["name", "age", "email", "skills"],
    "properties": {
        "name":   {"type": "string", "minLength": 1},
        "age":    {"type": "integer", "minimum": 0, "maximum": 150},
        "email":  {"type": "string", "pattern": r".+@.+\..+"},
        "skills": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "additionalProperties": False,
}

def validate_schema(data: Any, schema: dict) -> list[str]:
    """Simple schema validator without jsonschema dependency."""
    errors = []
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            return [f"Expected object, got {type(data).__name__}"]
        for field in schema.get("required", []):
            if field not in data:
                errors.append(f"Missing required field: '{field}'")
        for field, field_schema in schema.get("properties", {}).items():
            if field not in data:
                continue
            value = data[field]
            expected_type = field_schema.get("type")
            if expected_type == "string" and not isinstance(value, str):
                errors.append(f"'{field}' must be string, got {type(value).__name__}")
            elif expected_type == "integer" and not isinstance(value, int):
                errors.append(f"'{field}' must be integer, got {type(value).__name__}")
            elif expected_type == "array" and not isinstance(value, list):
                errors.append(f"'{field}' must be array, got {type(value).__name__}")
            if expected_type == "integer" and isinstance(value, int):
                if "minimum" in field_schema and value < field_schema["minimum"]:
                    errors.append(f"'{field}' {value} < minimum {field_schema['minimum']}")
                if "maximum" in field_schema and value > field_schema["maximum"]:
                    errors.append(f"'{field}' {value} > maximum {field_schema['maximum']}")
    return errors

@dataclass
class ValidationResult:
    data: Optional[dict]
    errors: list[str]
    attempts: int
    success: bool

def extract_with_validation(client: anthropic.Anthropic, text: str,
                             max_attempts: int = 3) -> ValidationResult:
    messages = [
        {"role": "user", "content": (
            f"Extract person info from this text as JSON matching this schema:\n"
            f"{json.dumps(EXTRACT_SCHEMA, indent=2)}\n\nText: {text}\n\n"
            "Return ONLY valid JSON, no markdown."
        )}
    ]

    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages,
        )
        raw = response.content[0].text.strip()

        # Parse JSON
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = json.loads(raw[start:end]) if start >= 0 else None
        except json.JSONDecodeError as e:
            errors = [f"JSON parse error: {e}"]
            data = None
        else:
            errors = validate_schema(data, EXTRACT_SCHEMA) if data else ["Empty output"]

        if not errors:
            print(f"[VALIDATE] Attempt {attempt}: OK")
            return ValidationResult(data, [], attempt, True)

        print(f"[VALIDATE] Attempt {attempt}: {errors}")

        # Repair prompt
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
            f"The output had these errors: {errors}\n"
            f"Fix and return only valid JSON matching the schema."})

    return ValidationResult(None, errors, max_attempts, False)

if __name__ == "__main__":
    client = anthropic.Anthropic()
    text = "Meet Alice Johnson, 29, developer at alice@example.com who knows Python and JavaScript."
    result = extract_with_validation(client, text)
    if result.success:
        print(f"\nExtracted in {result.attempts} attempt(s): {result.data}")
    else:
        print(f"\nFailed after {result.attempts} attempts: {result.errors}")

# Expected Token Savings: Catching errors early prevents downstream reprocessing costs
# Environment: pip install anthropic
```

### Option 2: Pydantic Schema Validation with Structured Output

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Optional, Any

# Pydantic-lite: manual field validation without the dependency
def validate_type(value: Any, expected: type, field_name: str) -> Optional[str]:
    if not isinstance(value, expected):
        return f"{field_name}: expected {expected.__name__}, got {type(value).__name__}"
    return None

@dataclass
class ProductAnalysis:
    product_name: str
    sentiment: str        # "positive" | "negative" | "neutral"
    score: float          # 0.0–1.0
    key_features: list[str]
    price_mentioned: bool
    recommendation: str

    @classmethod
    def from_dict(cls, data: dict) -> tuple["ProductAnalysis", list[str]]:
        errors = []
        # Required fields
        for field in ["product_name", "sentiment", "score", "key_features",
                      "price_mentioned", "recommendation"]:
            if field not in data:
                errors.append(f"Missing field: {field}")

        if errors:
            return None, errors  # type: ignore[return-value]

        # Type checks
        for field, expected in [("product_name", str), ("sentiment", str),
                                  ("recommendation", str), ("price_mentioned", bool)]:
            err = validate_type(data[field], expected, field)
            if err:
                errors.append(err)

        if not isinstance(data.get("score"), (int, float)):
            errors.append("score: expected float")
        elif not 0.0 <= float(data["score"]) <= 1.0:
            errors.append(f"score {data['score']} out of range [0.0, 1.0]")

        if not isinstance(data.get("key_features"), list):
            errors.append("key_features: expected array")
        elif not data["key_features"]:
            errors.append("key_features: must not be empty")

        valid_sentiments = {"positive", "negative", "neutral"}
        if data.get("sentiment") not in valid_sentiments:
            errors.append(f"sentiment must be one of {valid_sentiments}")

        if errors:
            return None, errors  # type: ignore[return-value]

        return cls(
            product_name=data["product_name"],
            sentiment=data["sentiment"],
            score=float(data["score"]),
            key_features=data["key_features"],
            price_mentioned=bool(data["price_mentioned"]),
            recommendation=data["recommendation"],
        ), []

ANALYSIS_PROMPT = """Analyze this product review and return JSON with these exact fields:
{
  "product_name": "string",
  "sentiment": "positive|negative|neutral",
  "score": 0.0-1.0,
  "key_features": ["list", "of", "mentioned", "features"],
  "price_mentioned": true|false,
  "recommendation": "brief recommendation string"
}

Return ONLY JSON, no markdown.

Review: {review}"""

def analyze_review(client: anthropic.Anthropic, review: str,
                   max_retries: int = 2) -> Optional[ProductAnalysis]:
    messages = [{"role": "user", "content": ANALYSIS_PROMPT.format(review=review)}]
    last_errors = []

    for attempt in range(1, max_retries + 2):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=messages,
        )
        raw = response.content[0].text.strip()

        try:
            start, end = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[start:end])
        except json.JSONDecodeError as e:
            last_errors = [f"JSON parse: {e}"]
            data = {}

        if data:
            obj, errors = ProductAnalysis.from_dict(data)
            if not errors:
                print(f"[VALIDATED] attempt={attempt} sentiment={obj.sentiment} "
                      f"score={obj.score:.2f}")
                return obj
            last_errors = errors

        print(f"[ERRORS] attempt={attempt}: {last_errors}")
        if attempt <= max_retries:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user",
                              "content": f"Fix these validation errors and resubmit: {last_errors}"})

    print(f"[FAILED] Could not produce valid output after {max_retries+1} attempts")
    return None

if __name__ == "__main__":
    client = anthropic.Anthropic()
    review = ("The TurboBlend 3000 is amazing! It blends smoothly and quietly. "
              "The price at $89 seems fair. Build quality is excellent. "
              "Highly recommend for smoothie lovers.")
    result = analyze_review(client, review)
    if result:
        print(f"\nResult: {result}")

# Expected Token Savings: Type-safe output prevents downstream reprocessing
# Environment: pip install anthropic
```

### Option 3: Async Parallel Validation with Multiple Schemas

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class SchemaField:
    name: str
    type: str                    # "string" | "integer" | "float" | "boolean" | "array" | "object"
    required: bool = True
    enum: Optional[list] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    min_length: Optional[int] = None

@dataclass
class Schema:
    name: str
    fields: list[SchemaField]

    def validate(self, data: dict) -> list[str]:
        errors = []
        for f in self.fields:
            if f.name not in data:
                if f.required:
                    errors.append(f"Missing: {f.name}")
                continue
            val = data[f.name]
            if f.type == "string" and not isinstance(val, str):
                errors.append(f"{f.name}: expected string")
            elif f.type == "integer" and not isinstance(val, int):
                errors.append(f"{f.name}: expected integer")
            elif f.type == "float" and not isinstance(val, (int, float)):
                errors.append(f"{f.name}: expected number")
            elif f.type == "boolean" and not isinstance(val, bool):
                errors.append(f"{f.name}: expected boolean")
            elif f.type == "array" and not isinstance(val, list):
                errors.append(f"{f.name}: expected array")
            if f.enum and val not in f.enum:
                errors.append(f"{f.name}: must be one of {f.enum}")
            if isinstance(val, str) and f.min_length and len(val) < f.min_length:
                errors.append(f"{f.name}: min length {f.min_length}")
            if isinstance(val, (int, float)):
                if f.min_val is not None and val < f.min_val:
                    errors.append(f"{f.name}: {val} < min {f.min_val}")
                if f.max_val is not None and val > f.max_val:
                    errors.append(f"{f.name}: {val} > max {f.max_val}")
        return errors

SCHEMAS = {
    "event": Schema("event", [
        SchemaField("title", "string", min_length=2),
        SchemaField("date", "string"),
        SchemaField("category", "string", enum=["conference", "webinar", "meetup", "workshop"]),
        SchemaField("capacity", "integer", min_val=1, max_val=100000),
        SchemaField("is_free", "boolean"),
    ]),
    "metric": Schema("metric", [
        SchemaField("name", "string"),
        SchemaField("value", "float"),
        SchemaField("unit", "string"),
        SchemaField("trend", "string", enum=["up", "down", "stable"]),
    ]),
}

async def extract_validated(client: anthropic.AsyncAnthropic,
                             text: str, schema_name: str) -> Optional[dict]:
    schema = SCHEMAS[schema_name]
    messages = [{"role": "user", "content":
        f"Extract {schema_name} info as JSON from: {text}\n"
        f"Required fields: {[f.name for f in schema.fields]}\n"
        f"Return ONLY JSON."}]

    for attempt in range(3):
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages,
        )
        raw = r.content[0].text.strip()
        try:
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            errors = schema.validate(data)
            if not errors:
                print(f"[OK:{schema_name}] attempt={attempt+1}")
                return data
            print(f"[INVALID:{schema_name}] attempt={attempt+1}: {errors}")
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Fix: {errors}. Return JSON only."}]
        except json.JSONDecodeError as e:
            print(f"[PARSE ERROR:{schema_name}] attempt={attempt+1}: {e}")
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "Invalid JSON. Return valid JSON only."}]
    return None

async def main():
    client = anthropic.AsyncAnthropic()
    tasks = [
        ("extract 'AI Summit 2026' conference on March 15 with 500 capacity, free entry", "event"),
        ("extract metric: response_time=245ms trending upward", "metric"),
        ("extract 'Python Meetup' workshop on April 3, 30 spots, paid", "event"),
    ]

    results = await asyncio.gather(*[
        extract_validated(client, text, schema)
        for text, schema in tasks
    ])

    for (text, schema), result in zip(tasks, results):
        status = "OK" if result else "FAILED"
        print(f"[{status}:{schema}] {text[:40]} → {json.dumps(result)[:60] if result else 'None'}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Parallel validation of multiple outputs reduces total round-trips
# Environment: pip install anthropic
```

### Option 4: Tool-Use Output Schema Enforcement

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any, Optional

# Enforce output schema via tool_choice=required
ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit structured text analysis results",
    "input_schema": {
        "type": "object",
        "required": ["summary", "sentiment", "key_points", "word_count_estimate", "language"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary",
                "minLength": 20,
            },
            "sentiment": {
                "type": "string",
                "enum": ["very_positive", "positive", "neutral", "negative", "very_negative"],
            },
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 5,
            },
            "word_count_estimate": {
                "type": "integer",
                "minimum": 1,
            },
            "language": {
                "type": "string",
                "description": "ISO 639-1 language code (e.g. en, es, fr)",
            },
        },
    }
}

@dataclass
class AnalysisResult:
    summary: str
    sentiment: str
    key_points: list[str]
    word_count_estimate: int
    language: str

def analyze_text(client: anthropic.Anthropic, text: str) -> Optional[AnalysisResult]:
    """Force structured output via tool_choice=required."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        tools=[ANALYSIS_TOOL],
        tool_choice={"type": "tool", "name": "submit_analysis"},
        messages=[{
            "role": "user",
            "content": f"Analyze this text and call submit_analysis:\n\n{text[:1000]}"
        }],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            inp = block.input
            # Validate the tool input (model is constrained by schema but verify anyway)
            errors = []
            if len(inp.get("summary", "")) < 20:
                errors.append("summary too short")
            if inp.get("sentiment") not in ANALYSIS_TOOL["input_schema"]["properties"]["sentiment"]["enum"]:
                errors.append("invalid sentiment")
            if not (2 <= len(inp.get("key_points", [])) <= 5):
                errors.append("key_points count out of range")

            if errors:
                print(f"[SCHEMA WARN] Tool output violations: {errors}")
            else:
                print(f"[SCHEMA OK] sentiment={inp['sentiment']} "
                      f"language={inp['language']} points={len(inp['key_points'])}")
            return AnalysisResult(
                summary=inp.get("summary", ""),
                sentiment=inp.get("sentiment", "neutral"),
                key_points=inp.get("key_points", []),
                word_count_estimate=inp.get("word_count_estimate", 0),
                language=inp.get("language", "en"),
            )

    print("[ERROR] No tool_use block in response")
    return None

if __name__ == "__main__":
    client = anthropic.Anthropic()
    texts = [
        ("Great! The new features are fantastic and the performance has improved dramatically. "
         "I'm really happy with this update."),
        ("The installation failed twice and customer support was unhelpful. "
         "Wasted 3 hours on this. Not recommended."),
    ]
    for text in texts:
        print(f"\nText: {text[:60]}...")
        result = analyze_text(client, text)
        if result:
            print(f"  Sentiment: {result.sentiment}")
            print(f"  Summary:   {result.summary[:70]}")
            print(f"  Points:    {result.key_points}")

# Expected Token Savings: tool_choice=required eliminates prose preamble; schema enforcement is free
# Environment: pip install anthropic
```

### Option 5: Incremental JSON Stream Validation

```python
import anthropic
import json
import re
from dataclasses import dataclass
from typing import Optional, Generator

REQUIRED_FIELDS = {"title", "category", "priority", "description", "assignee"}
FIELD_TYPES = {"title": str, "category": str, "priority": int,
               "description": str, "assignee": str}
VALID_CATEGORIES = {"bug", "feature", "improvement", "security"}
PRIORITY_RANGE = (1, 5)

@dataclass
class TicketData:
    title: str
    category: str
    priority: int
    description: str
    assignee: str

def validate_ticket(data: dict) -> list[str]:
    errors = []
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"Missing fields: {missing}")
        return errors  # Can't validate further

    for field, expected_type in FIELD_TYPES.items():
        if not isinstance(data[field], expected_type):
            errors.append(f"{field}: expected {expected_type.__name__}, "
                          f"got {type(data[field]).__name__}")

    if data.get("category") not in VALID_CATEGORIES:
        errors.append(f"category must be one of {VALID_CATEGORIES}")

    p = data.get("priority")
    if isinstance(p, int) and not (PRIORITY_RANGE[0] <= p <= PRIORITY_RANGE[1]):
        errors.append(f"priority {p} out of range {PRIORITY_RANGE}")

    if isinstance(data.get("title"), str) and len(data["title"]) < 5:
        errors.append("title must be at least 5 characters")

    return errors

def stream_and_validate(client: anthropic.Anthropic, user_request: str,
                          max_repairs: int = 2) -> Optional[TicketData]:
    prompt = (f"Create a bug ticket as JSON with fields: "
              f"title, category (bug/feature/improvement/security), "
              f"priority (1-5 integer), description, assignee.\n"
              f"Return ONLY JSON.\n\nRequest: {user_request}")
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_repairs + 1):
        print(f"[STREAM] Attempt {attempt+1}...", end=" ")
        full_text = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                full_text += text
                print(".", end="", flush=True)

        print()
        try:
            s, e = full_text.find("{"), full_text.rfind("}") + 1
            data = json.loads(full_text[s:e]) if s >= 0 else {}
        except json.JSONDecodeError as exc:
            errors = [f"JSON parse: {exc}"]
            data = {}
        else:
            errors = validate_ticket(data)

        if not errors:
            print(f"[VALID] Fields: {list(data.keys())}")
            return TicketData(
                title=data["title"], category=data["category"],
                priority=data["priority"], description=data["description"],
                assignee=data["assignee"],
            )

        print(f"[INVALID] {errors}")
        messages.append({"role": "assistant", "content": full_text})
        messages.append({"role": "user",
                          "content": f"Fix these errors: {errors}. Return only JSON."})

    return None

if __name__ == "__main__":
    client = anthropic.Anthropic()
    requests = [
        "Login button crashes on mobile for iOS 17 users. Assign to team-mobile, high priority.",
        "Add dark mode support to dashboard. Medium priority for frontend team.",
    ]
    for req in requests:
        print(f"\nRequest: {req[:60]}")
        ticket = stream_and_validate(client, req)
        if ticket:
            print(f"Ticket: {ticket}")

# Expected Token Savings: Streaming detects early truncation; repair only costs one extra call
# Environment: pip install anthropic
```

### Option 6: Schema Registry with Versioned Migrations

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Callable

@dataclass
class SchemaVersion:
    version: int
    fields: dict[str, type]
    required: list[str]
    migrate_from: Optional[Callable[[dict], dict]] = None

class SchemaRegistry:
    """Validates outputs against versioned schemas with forward migration."""
    def __init__(self):
        self._schemas: dict[str, list[SchemaVersion]] = {}

    def register(self, name: str, version: SchemaVersion) -> None:
        self._schemas.setdefault(name, []).append(version)
        self._schemas[name].sort(key=lambda s: s.version)

    def latest(self, name: str) -> Optional[SchemaVersion]:
        schemas = self._schemas.get(name, [])
        return schemas[-1] if schemas else None

    def validate(self, name: str, data: dict) -> tuple[bool, list[str], dict]:
        """Returns (valid, errors, migrated_data)."""
        latest = self.latest(name)
        if not latest:
            return False, [f"Unknown schema: {name}"], data

        # Try to detect version and migrate if needed
        data_version = data.get("_schema_version", 1)
        migrated = data.copy()

        for schema in self._schemas[name]:
            if schema.version > data_version and schema.migrate_from:
                migrated = schema.migrate_from(migrated)
                migrated["_schema_version"] = schema.version

        errors = []
        for field_name in latest.required:
            if field_name not in migrated:
                errors.append(f"Missing required: {field_name}")

        for field_name, expected_type in latest.fields.items():
            if field_name in migrated and not isinstance(migrated[field_name], expected_type):
                errors.append(f"{field_name}: expected {expected_type.__name__}, "
                               f"got {type(migrated[field_name]).__name__}")

        return not errors, errors, migrated

# Register schemas
registry = SchemaRegistry()

# v1 schema
registry.register("report", SchemaVersion(
    version=1,
    fields={"title": str, "content": str, "score": float},
    required=["title", "content", "score"],
))

# v2 schema — adds "tags" field, migrates from v1
def migrate_v1_to_v2(data: dict) -> dict:
    data.setdefault("tags", [])
    data.setdefault("author", "unknown")
    return data

registry.register("report", SchemaVersion(
    version=2,
    fields={"title": str, "content": str, "score": float, "tags": list, "author": str},
    required=["title", "content", "score", "tags", "author"],
    migrate_from=migrate_v1_to_v2,
))

REPORT_PROMPT = """Generate a quality report as JSON with fields:
title (string), content (2+ sentences), score (0.0-1.0 float),
tags (list of strings), author (string).

Return ONLY JSON. Topic: {topic}"""

def generate_report(client: anthropic.Anthropic, topic: str,
                     max_retries: int = 2) -> Optional[dict]:
    messages = [{"role": "user", "content": REPORT_PROMPT.format(topic=topic)}]

    for attempt in range(max_retries + 1):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=messages,
        )
        raw = r.content[0].text.strip()
        try:
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
        except json.JSONDecodeError as exc:
            print(f"[PARSE] Attempt {attempt+1}: {exc}")
            messages += [{"role": "assistant", "content": raw},
                          {"role": "user", "content": "Invalid JSON. Return valid JSON only."}]
            continue

        valid, errors, migrated = registry.validate("report", data)
        if valid:
            schema_v = migrated.get("_schema_version", 1)
            print(f"[VALID] Attempt {attempt+1} schema=v{schema_v} "
                  f"score={migrated.get('score')} tags={migrated.get('tags')}")
            return migrated

        print(f"[INVALID] Attempt {attempt+1}: {errors}")
        messages += [{"role": "assistant", "content": raw},
                      {"role": "user", "content": f"Fix: {errors}. Return JSON only."}]

    return None

if __name__ == "__main__":
    client = anthropic.Anthropic()
    topics = ["quarterly performance of the AI products team",
              "code quality review for the auth module"]
    for topic in topics:
        print(f"\nTopic: {topic}")
        report = generate_report(client, topic)
        if report:
            print(f"  Title:  {report.get('title', '')[:50]}")
            print(f"  Score:  {report.get('score')}")
            print(f"  Author: {report.get('author')}")
            print(f"  Tags:   {report.get('tags')}")

# Expected Token Savings: Schema registry prevents costly downstream failures from bad data
# Environment: pip install anthropic
```

## Comparison

| Option | Validation Method | Auto-Repair | Schema Source | Best For |
|--------|------------------|-------------|---------------|----------|
| 1. Manual JSON Schema | Custom validator | Repair prompt | Inline dict | Lightweight, no deps |
| 2. Pydantic-style | Field-by-field | Repair prompt | Dataclass | Type-safe extraction |
| 3. Async Parallel | Per-field checks | Repair prompt | Schema objects | Batch validation |
| 4. Tool-use enforcement | Claude's own schema | N/A (constrained) | Tool definition | Strictest enforcement |
| 5. Stream + validate | Post-stream | Repair prompt | Manual | Streaming output |
| 6. Versioned registry | Registry lookup | Repair prompt | Versioned schema | Evolving APIs |
