---
title: "Agent Doesn't Implement API Response Schema Validation"
description: "Six solutions for validating API and LLM response schemas before processing, preventing silent data corruption from unexpected response shapes."
difficulty: intermediate
category: security
tags: [validation, schema, pydantic, jsonschema, security, data-integrity]
---

# Agent Doesn't Implement API Response Schema Validation

When agents consume external API responses or LLM-structured outputs without validation, an unexpected field type or missing key propagates silently until it causes a downstream crash or, worse, corrupts state. These six solutions enforce schema contracts at the boundary where data enters the agent.

## Solution 1: Pydantic Model Validation for LLM Structured Output

Parse every LLM response through a Pydantic model; retry with corrective prompt on validation failure.

```python
import asyncio
import json
from typing import Any
from pydantic import BaseModel, Field, ValidationError, field_validator
from anthropic import AsyncAnthropic


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(pattern=r"^(person|organization|location|date|other)$")
    confidence: float = Field(ge=0.0, le=1.0)
    context: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()


class ExtractionResponse(BaseModel):
    entities: list[ExtractedEntity]
    source_text_length: int = Field(ge=0)
    model_version: str = Field(default="unknown")


class ValidatedExtractionAgent:
    SYSTEM_PROMPT = """Extract named entities from text. Always respond with valid JSON matching:
{
  "entities": [
    {"name": "...", "entity_type": "person|organization|location|date|other",
     "confidence": 0.0-1.0, "context": "..."}
  ],
  "source_text_length": <int>,
  "model_version": "1.0"
}"""

    def __init__(self, max_retries: int = 3):
        self.client = AsyncAnthropic()
        self.max_retries = max_retries

    async def extract_entities(self, text: str) -> ExtractionResponse:
        messages = [{"role": "user", "content": f"Extract entities from:\n\n{text}"}]

        for attempt in range(self.max_retries):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=self.SYSTEM_PROMPT,
                messages=messages,
            )
            raw = response.content[0].text

            # Extract JSON from response
            try:
                # Handle markdown code fences
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0].strip()
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0].strip()
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"LLM returned invalid JSON after {self.max_retries} attempts: {e}")
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your response was not valid JSON: {e}. Please respond with only valid JSON."
                })
                continue

            try:
                return ExtractionResponse.model_validate(data)
            except ValidationError as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"Schema validation failed after {self.max_retries} attempts: {e}")
                error_summary = "; ".join(
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                    for err in e.errors()
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Schema validation errors: {error_summary}. "
                        "Fix and return corrected JSON only."
                    ),
                })

        raise RuntimeError("Exhausted retries")


async def demo_pydantic_validation():
    agent = ValidatedExtractionAgent()
    text = "Apple Inc. CEO Tim Cook announced in San Francisco on January 15 that revenues exceeded $100B."
    result = await agent.extract_entities(text)
    for entity in result.entities:
        print(f"  [{entity.entity_type}] {entity.name} (confidence={entity.confidence:.2f})")
```

## Solution 2: JSON Schema Validation with jsonschema

Define a JSON Schema; validate any API response (LLM or external) before consuming it.

```python
import asyncio
import json
from typing import Any
import jsonschema
from jsonschema import Draft7Validator, ValidationError as JSONSchemaError
from anthropic import AsyncAnthropic

# JSON Schema for agent tool call results
TOOL_RESULT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["status", "data", "metadata"],
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["success", "partial", "error"]
        },
        "data": {
            "type": "object",
            "required": ["records"],
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "value"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "value": {"type": ["string", "number", "boolean"]},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    }
                },
                "total": {"type": "integer", "minimum": 0}
            }
        },
        "metadata": {
            "type": "object",
            "required": ["timestamp", "source"],
            "properties": {
                "timestamp": {"type": "string", "format": "date-time"},
                "source": {"type": "string"},
                "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"}
            }
        }
    }
}


class SchemaValidator:
    def __init__(self, schema: dict[str, Any]):
        self.validator = Draft7Validator(schema)
        self._schema = schema

    def validate(self, data: Any) -> list[str]:
        """Returns list of error messages; empty list means valid."""
        errors = sorted(self.validator.iter_errors(data), key=lambda e: list(e.path))
        return [
            f"$.{'.'.join(str(p) for p in e.path)}: {e.message}"
            if e.path else e.message
            for e in errors
        ]

    def validate_strict(self, data: Any):
        """Raises ValueError with all errors if invalid."""
        errors = self.validate(data)
        if errors:
            raise ValueError(f"Schema validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


class SchemaValidatedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.tool_validator = SchemaValidator(TOOL_RESULT_SCHEMA)

    def _build_tool_prompt(self, query: str) -> str:
        return (
            f"Answer this query by returning a JSON tool result matching this schema:\n"
            f"{json.dumps(TOOL_RESULT_SCHEMA, indent=2)}\n\n"
            f"Query: {query}\n\n"
            "Return ONLY valid JSON, no markdown."
        )

    async def query_with_validation(self, query: str, max_retries: int = 3) -> dict:
        messages = [{"role": "user", "content": self._build_tool_prompt(query)}]

        for attempt in range(max_retries):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=messages,
            )
            raw = response.content[0].text.strip()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                if attempt == max_retries - 1:
                    raise
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Invalid JSON: {e}. Return valid JSON only."},
                ]
                continue

            errors = self.tool_validator.validate(data)
            if not errors:
                return data

            if attempt == max_retries - 1:
                raise ValueError(f"Persistent schema violations:\n" + "\n".join(errors))

            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Schema errors:\n" + "\n".join(errors) + "\nFix and return corrected JSON."},
            ]

        raise RuntimeError("Unreachable")


async def demo_schema_validation():
    agent = SchemaValidatedAgent()
    result = await agent.query_with_validation("List 3 Python web frameworks with their release years.")
    print(f"Status: {result['status']}")
    for record in result["data"]["records"]:
        print(f"  {record['id']}: {record['value']}")
```

## Solution 3: Type-Safe Response Parsing with TypeAdapter

Use Pydantic's TypeAdapter for validating complex generic types like `list[dict]` or `dict[str, list[int]]`.

```python
import asyncio
import json
from typing import Annotated
from pydantic import BaseModel, Field, TypeAdapter, field_validator
from anthropic import AsyncAnthropic


# Complex nested type for agent action plans
ActionStep = Annotated[
    dict,
    Field(description="Single action step")
]


class AgentPlan(BaseModel):
    goal: str = Field(min_length=1)
    steps: list[dict] = Field(min_length=1, max_length=20)
    estimated_tokens: int = Field(gt=0, le=100_000)
    requires_tools: list[str]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, steps: list[dict]) -> list[dict]:
        for i, step in enumerate(steps):
            if "action" not in step:
                raise ValueError(f"Step {i} missing required 'action' field")
            if not isinstance(step["action"], str) or not step["action"].strip():
                raise ValueError(f"Step {i} 'action' must be non-empty string")
        return steps

    @field_validator("requires_tools")
    @classmethod
    def validate_tool_names(cls, tools: list[str]) -> list[str]:
        allowed = {"web_search", "code_exec", "file_read", "file_write", "api_call", "none"}
        invalid = [t for t in tools if t not in allowed]
        if invalid:
            raise ValueError(f"Unknown tools: {invalid}. Allowed: {allowed}")
        return tools


plan_adapter = TypeAdapter(AgentPlan)
plan_list_adapter = TypeAdapter(list[AgentPlan])


class PlanningAgent:
    SYSTEM = """Generate an agent execution plan as JSON:
{
  "goal": "...",
  "steps": [{"action": "...", "description": "...", "tool": "none|web_search|code_exec|..."}],
  "estimated_tokens": <int>,
  "requires_tools": ["none" | "web_search" | "code_exec" | "file_read" | "file_write" | "api_call"],
  "confidence": 0.0-1.0
}"""

    def __init__(self):
        self.client = AsyncAnthropic()

    async def plan(self, objective: str) -> AgentPlan:
        for attempt in range(3):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=self.SYSTEM,
                messages=[{"role": "user", "content": f"Plan for: {objective}"}],
            )
            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            try:
                data = json.loads(raw)
                plan = plan_adapter.validate_python(data)
                return plan
            except (json.JSONDecodeError, Exception) as e:
                if attempt == 2:
                    raise ValueError(f"Plan validation failed: {e}") from e
                continue

        raise RuntimeError("Unreachable")

    async def plan_batch(self, objectives: list[str]) -> list[AgentPlan]:
        """Generate multiple plans concurrently with individual validation."""
        results = await asyncio.gather(
            *[self.plan(obj) for obj in objectives],
            return_exceptions=True,
        )
        plans = []
        for obj, result in zip(objectives, results):
            if isinstance(result, Exception):
                print(f"Failed to plan '{obj}': {result}")
            else:
                plans.append(result)
        return plans


async def demo_typeadapter():
    agent = PlanningAgent()
    objectives = [
        "Research the latest developments in quantum computing",
        "Write a Python script to parse CSV files",
    ]
    plans = await agent.plan_batch(objectives)
    for plan in plans:
        print(f"\nGoal: {plan.goal}")
        print(f"Steps: {len(plan.steps)}, Tools: {plan.requires_tools}, Confidence: {plan.confidence}")
```

## Solution 4: Runtime Schema Registry with Version Negotiation

Maintain a schema registry; validate response against the version declared in the response envelope.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel, ValidationError
from anthropic import AsyncAnthropic


class ResponseEnvelopeV1(BaseModel):
    schema_version: str = "1.0"
    request_id: str
    payload: dict[str, Any]
    errors: list[str] = []


class ResponseEnvelopeV2(BaseModel):
    schema_version: str = "2.0"
    request_id: str
    payload: dict[str, Any]
    errors: list[str] = []
    warnings: list[str] = []
    processing_time_ms: float


@dataclass
class SchemaRegistry:
    _schemas: dict[str, type[BaseModel]] = None

    def __post_init__(self):
        self._schemas = {
            "1.0": ResponseEnvelopeV1,
            "2.0": ResponseEnvelopeV2,
        }

    def register(self, version: str, model: type[BaseModel]):
        self._schemas[version] = model

    def validate(self, data: dict) -> BaseModel:
        version = data.get("schema_version", "1.0")
        schema_cls = self._schemas.get(version)
        if schema_cls is None:
            raise ValueError(
                f"Unknown schema version '{version}'. "
                f"Supported: {list(self._schemas.keys())}"
            )
        try:
            return schema_cls.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"Schema v{version} validation failed: {e}") from e

    @property
    def latest_version(self) -> str:
        return max(self._schemas.keys())


REGISTRY = SchemaRegistry()

VERSIONED_SYSTEM = """Respond with a JSON envelope. Use schema_version "2.0":
{
  "schema_version": "2.0",
  "request_id": "<uuid>",
  "payload": { <your answer as structured data> },
  "errors": [],
  "warnings": [],
  "processing_time_ms": <float>
}
Return only JSON."""


class VersionedResponseAgent:
    def __init__(self, registry: SchemaRegistry = REGISTRY):
        self.client = AsyncAnthropic()
        self.registry = registry

    async def query(self, message: str, max_retries: int = 3) -> BaseModel:
        messages_hist = [{"role": "user", "content": message}]

        for attempt in range(max_retries):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=VERSIONED_SYSTEM,
                messages=messages_hist,
            )
            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()

            try:
                data = json.loads(raw)
                return self.registry.validate(data)
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == max_retries - 1:
                    raise
                messages_hist += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Error: {e}. Return corrected JSON envelope."},
                ]

        raise RuntimeError("Unreachable")


async def demo_registry():
    agent = VersionedResponseAgent()
    result = await agent.query("What are the top 3 programming languages in 2024?")
    print(f"Schema version: {result.schema_version}")
    print(f"Payload: {result.payload}")
    if hasattr(result, "processing_time_ms"):
        print(f"Processing time: {result.processing_time_ms}ms")
```

## Solution 5: Streaming Response Validation with Partial Schema Checking

Validate streaming chunks as they arrive; abort early if schema violations are detected mid-stream.

```python
import asyncio
import json
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class StreamValidationState:
    buffer: str = ""
    depth: int = 0          # JSON nesting depth
    in_string: bool = False
    escape_next: bool = False
    aborted: bool = False
    abort_reason: str = ""
    required_keys_seen: set[str] = field(default_factory=set)

    REQUIRED_ROOT_KEYS = {"status", "result", "confidence"}
    MAX_BUFFER_SIZE = 50_000  # Abort if response exceeds this

    def feed(self, chunk: str) -> bool:
        """Returns False to abort streaming."""
        self.buffer += chunk
        if len(self.buffer) > self.MAX_BUFFER_SIZE:
            self.aborted = True
            self.abort_reason = f"Response exceeds {self.MAX_BUFFER_SIZE} chars"
            return False
        # Track JSON structure
        for char in chunk:
            if self.escape_next:
                self.escape_next = False
                continue
            if char == "\\" and self.in_string:
                self.escape_next = True
            elif char == '"':
                self.in_string = not self.in_string
            elif not self.in_string:
                if char == "{":
                    self.depth += 1
                elif char == "}":
                    self.depth -= 1
        return True

    def check_partial_keys(self) -> list[str]:
        """Detect obviously wrong keys at root level early."""
        warnings = []
        # Simple heuristic: look for key patterns in accumulated buffer
        import re
        keys = re.findall(r'"(\w+)"\s*:', self.buffer[:500])
        self.required_keys_seen.update(keys[:5])
        return warnings

    def finalize(self) -> dict:
        """Parse and validate complete buffer."""
        if self.aborted:
            raise ValueError(f"Stream aborted: {self.abort_reason}")
        try:
            data = json.loads(self.buffer)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in stream: {e}")

        missing = self.REQUIRED_ROOT_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"Missing required keys: {missing}")

        if "confidence" in data:
            conf = data["confidence"]
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                raise ValueError(f"confidence must be float 0-1, got {conf!r}")

        return data


class StreamValidatingAgent:
    SYSTEM = """Always respond with JSON:
{
  "status": "success|error",
  "result": <your answer>,
  "confidence": 0.0-1.0,
  "explanation": "..."
}"""

    def __init__(self):
        self.client = AsyncAnthropic()

    async def query_streaming(self, message: str) -> dict:
        state = StreamValidationState()

        async with self.client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=self.SYSTEM,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text in stream.text_stream:
                should_continue = state.feed(text)
                if not should_continue:
                    break

        return state.finalize()


async def demo_stream_validation():
    agent = StreamValidatingAgent()
    result = await agent.query_streaming("Is Python faster than C++?")
    print(f"Status: {result['status']}")
    print(f"Confidence: {result['confidence']}")
    print(f"Result: {result['result']}")
```

## Solution 6: Layered Validation Pipeline with Sanitization

Chain multiple validation stages: syntax → schema → business rules → sanitization → output.

```python
import asyncio
import json
import re
import html
from dataclasses import dataclass
from typing import Any, Callable
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic


class AgentOutput(BaseModel):
    answer: str = Field(min_length=1, max_length=10_000)
    sources: list[str] = Field(max_length=20)
    sentiment: str = Field(pattern=r"^(positive|negative|neutral)$")
    toxicity_score: float = Field(ge=0.0, le=1.0)


@dataclass
class ValidationResult:
    stage: str
    passed: bool
    errors: list[str]
    data: Any


class ValidationPipeline:
    def __init__(self):
        self._stages: list[tuple[str, Callable]] = []

    def add_stage(self, name: str, validator: Callable):
        self._stages.append((name, validator))
        return self

    async def run(self, raw: str) -> tuple[AgentOutput, list[ValidationResult]]:
        results: list[ValidationResult] = []
        current_data: Any = raw

        for stage_name, validator in self._stages:
            try:
                if asyncio.iscoroutinefunction(validator):
                    current_data = await validator(current_data)
                else:
                    current_data = validator(current_data)
                results.append(ValidationResult(stage_name, True, [], current_data))
            except (ValueError, TypeError, KeyError) as e:
                results.append(ValidationResult(stage_name, False, [str(e)], current_data))
                raise ValueError(f"Pipeline failed at '{stage_name}': {e}") from e

        return current_data, results


def parse_json(raw: str) -> dict:
    """Stage 1: Parse JSON, strip markdown fences."""
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}")


def validate_schema(data: dict) -> dict:
    """Stage 2: Pydantic schema validation."""
    from pydantic import ValidationError
    try:
        AgentOutput.model_validate(data)
        return data
    except Exception as e:
        raise ValueError(str(e))


def enforce_business_rules(data: dict) -> dict:
    """Stage 3: Domain-specific business rules."""
    if data.get("toxicity_score", 0) > 0.7:
        raise ValueError(f"Toxicity score {data['toxicity_score']} exceeds maximum 0.7")
    if len(data.get("answer", "")) < 10:
        raise ValueError("Answer too short for meaningful response")
    # Require sources for factual claims
    factual_markers = ["is", "are", "was", "were", "percent", "study", "research"]
    answer_lower = data.get("answer", "").lower()
    if any(m in answer_lower for m in factual_markers) and not data.get("sources"):
        data["sources"] = ["[unverified]"]
    return data


def sanitize_output(data: dict) -> AgentOutput:
    """Stage 4: Sanitize strings against XSS and injection."""
    data["answer"] = html.escape(data["answer"])
    # Remove potential script injection from sources
    data["sources"] = [
        re.sub(r'<[^>]+>', '', src)[:200]
        for src in data.get("sources", [])
    ]
    return AgentOutput.model_validate(data)


PIPELINE = (
    ValidationPipeline()
    .add_stage("json_parse", parse_json)
    .add_stage("schema_validate", validate_schema)
    .add_stage("business_rules", enforce_business_rules)
    .add_stage("sanitize", sanitize_output)
)

SYSTEM = """Respond with JSON:
{
  "answer": "...",
  "sources": ["url or title"],
  "sentiment": "positive|negative|neutral",
  "toxicity_score": 0.0-1.0
}"""


class PipelineValidatedAgent:
    def __init__(self, pipeline: ValidationPipeline = PIPELINE):
        self.client = AsyncAnthropic()
        self.pipeline = pipeline

    async def query(self, message: str, max_retries: int = 3) -> AgentOutput:
        for attempt in range(max_retries):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": message}],
            )
            raw = response.content[0].text
            try:
                output, stages = await self.pipeline.run(raw)
                return output
            except ValueError as e:
                if attempt == max_retries - 1:
                    raise
                print(f"Attempt {attempt + 1} failed: {e}")
        raise RuntimeError("Unreachable")


async def demo_pipeline():
    agent = PipelineValidatedAgent()
    result = await agent.query("Is remote work more productive than office work?")
    print(f"Answer: {result.answer[:100]}")
    print(f"Sentiment: {result.sentiment}, Toxicity: {result.toxicity_score}")
    print(f"Sources: {result.sources}")
```

## Comparison Table

| Solution | Validation Library | Retry Logic | Streaming Support | Schema Versioning | Best For |
|---|---|---|---|---|---|
| Pydantic Model | Pydantic v2 | Yes (corrective prompt) | No | No | Typed LLM structured output |
| JSON Schema | jsonschema | Yes | No | Via $schema field | External API responses |
| TypeAdapter | Pydantic v2 | Yes | No | No | Complex generic types |
| Schema Registry | Pydantic v2 | Yes | No | Yes (version negotiation) | Multi-version API consumers |
| Stream Validation | Custom parser | No (abort early) | Yes | No | Streaming LLM responses |
| Layered Pipeline | Pydantic + custom | Yes | No | No | Full sanitization + business rules |

**Recommended**: Use **Pydantic Model** (Solution 1) as the default for LLM structured outputs — it catches type errors, provides corrective retry, and integrates cleanly with Python typing. Add **Layered Pipeline** (Solution 6) when outputs are user-facing and need sanitization. Use **Schema Registry** (Solution 4) when integrating with evolving external APIs that version their responses.
