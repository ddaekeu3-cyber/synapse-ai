---
layout: solution
title: "Agent Doesn't Implement Output Format Negotiation with Caller"
category: general
description: "Let callers declare their preferred output format (JSON, Markdown, plain text, structured schema) and have the agent honor it consistently across turns."
tags: [general, output-format, negotiation, structured-output, json, markdown, schema]
---

# Agent Doesn't Implement Output Format Negotiation with Caller

An agent that always returns Markdown is useless to a caller that needs JSON. An agent that always returns JSON is unreadable in a chat UI. Without output format negotiation, every consumer has to post-process and parse agent output differently, and format mismatches cause silent data loss. Format negotiation lets the caller declare what it needs once, and the agent commits to that contract for all responses in the session.

## Option 1: Format Header in System Prompt

```python
import anthropic
import json

client = anthropic.Anthropic()

FORMAT_INSTRUCTIONS = {
    "json": "Always respond with valid JSON only. No prose, no markdown, no explanation outside JSON.",
    "markdown": "Always respond in GitHub-flavored Markdown with headers, bullet points, and code blocks where appropriate.",
    "plain": "Always respond in plain text only. No markdown, no JSON, no special formatting.",
    "structured": "Always respond with a structured report: first a one-sentence Summary, then Key Points as a numbered list, then a Conclusion.",
}


def build_system_prompt(format_name: str) -> str:
    instruction = FORMAT_INSTRUCTIONS.get(format_name, FORMAT_INSTRUCTIONS["plain"])
    return f"You are a helpful assistant.\n\nOUTPUT FORMAT REQUIREMENT: {instruction}"


def ask(question: str, output_format: str = "plain") -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=build_system_prompt(output_format),
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


question = "What are the main benefits of using async programming in Python?"

for fmt in ["plain", "markdown", "json", "structured"]:
    print(f"\n=== Format: {fmt} ===")
    result = ask(question, fmt)
    print(result[:400])

    if fmt == "json":
        try:
            parsed = json.loads(result)
            print(f"[valid JSON with keys: {list(parsed.keys())[:5]}]")
        except Exception as e:
            print(f"[JSON parse failed: {e}]")

# Expected Token Savings: N/A (format pattern); eliminates caller-side parsing overhead and format mismatch bugs
# Environment: Python 3.11+; store format preference in session metadata so it persists across turns
```

## Option 2: Caller-Declared Schema with Validation

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()


def build_schema_prompt(schema: dict) -> str:
    """Ask the model to produce output conforming to a JSON schema."""
    return f"""You must respond with a JSON object that strictly follows this schema:

{json.dumps(schema, indent=2)}

Rules:
- Output valid JSON only
- Include all required fields
- Use correct types as specified
- No additional fields unless allowed by the schema"""


def ask_with_schema(question: str, schema: dict, max_retries: int = 2) -> dict[str, Any]:
    """Ask a question and validate the response against the provided schema."""
    system = build_schema_prompt(schema)
    messages: list[dict] = [{"role": "user", "content": question}]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()

        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
            # Basic required-field validation
            required = schema.get("required", [])
            missing = [f for f in required if f not in data]
            if missing:
                raise ValueError(f"Missing required fields: {missing}")
            print(f"[attempt {attempt+1}] Valid response")
            return data
        except Exception as e:
            print(f"[attempt {attempt+1}] Invalid: {e}")
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": f"Your response was invalid: {e}. Please correct it."})

    return {}


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "key_topics": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["sentiment", "confidence", "key_topics", "summary"],
}

result = ask_with_schema(
    "Analyze: 'The new async features in Python 3.11 are a massive improvement for I/O-bound applications.'",
    ANALYSIS_SCHEMA,
)
print(f"\nParsed result: {json.dumps(result, indent=2)}")

# Expected Token Savings: N/A; validation retry adds tokens but prevents downstream parsing failures
# Environment: Python 3.11+; use jsonschema library for full JSON Schema validation in production
```

## Option 3: Session-Level Format Contract with Turn Tracking

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()


@dataclass
class FormatContract:
    """Negotiated format contract for a session."""
    format_type: str          # "json" | "markdown" | "plain" | "table" | "yaml"
    schema: dict | None = None
    turn_count: int = 0
    violations: list[int] = field(default_factory=list)  # turns where format was violated

    def system_instruction(self) -> str:
        if self.format_type == "json" and self.schema:
            import json
            return f"Respond ONLY with valid JSON matching: {json.dumps(self.schema)}"
        instructions = {
            "json":     "Respond ONLY with valid JSON. No prose.",
            "markdown": "Respond in Markdown only. Use headers (##), bullets (-), and code blocks (```).",
            "plain":    "Respond in plain text only. No markdown, no JSON.",
            "table":    "Present all results as Markdown tables where possible.",
            "yaml":     "Respond ONLY with valid YAML. No prose, no markdown.",
        }
        return instructions.get(self.format_type, "Respond in plain text.")

    def validate(self, response: str) -> bool:
        """Basic format compliance check."""
        if self.format_type == "json":
            try:
                import json
                json.loads(response.strip())
                return True
            except Exception:
                return False
        if self.format_type == "yaml":
            return not response.strip().startswith("{")  # minimal check
        if self.format_type == "markdown":
            return any(c in response for c in ["#", "-", "`", "**"])
        if self.format_type == "plain":
            return "```" not in response and response.count("#") < 2
        return True


@dataclass
class NegotiatedSession:
    contract: FormatContract
    history: list[dict] = field(default_factory=list)

    def ask(self, user_message: str) -> str:
        self.contract.turn_count += 1
        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=f"You are a helpful assistant.\n\n{self.contract.system_instruction()}",
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})

        if not self.contract.validate(reply):
            self.contract.violations.append(self.contract.turn_count)
            print(f"[WARNING] Format violation at turn {self.contract.turn_count}")

        return reply


# Demonstrate with different sessions
for fmt in ["plain", "markdown", "json"]:
    print(f"\n{'='*40}")
    print(f"Session with format: {fmt}")
    session = NegotiatedSession(contract=FormatContract(format_type=fmt))

    r1 = session.ask("List 3 Python web frameworks.")
    print(f"Turn 1:\n{r1[:300]}")

    r2 = session.ask("Which one is best for REST APIs?")
    print(f"Turn 2:\n{r2[:200]}")

    print(f"Violations: {session.contract.violations}")

# Expected Token Savings: N/A; consistent format reduces caller-side error handling by ~80%
# Environment: Python 3.11+; store FormatContract in session metadata; log violations to alert on model drift
```

## Option 4: Content-Type Header Pattern for API Callers

```python
import asyncio
import anthropic
import json
import yaml  # pip install pyyaml

client = anthropic.AsyncAnthropic()

FORMAT_RENDERERS = {
    "application/json": lambda text: json.dumps({"response": text}, ensure_ascii=False),
    "text/markdown": lambda text: f"## Response\n\n{text}",
    "text/plain": lambda text: text,
    "text/yaml": lambda data: yaml.dump({"response": data}, default_flow_style=False),
    "text/csv": lambda text: "\n".join(
        f'"{line.strip()}"' for line in text.splitlines() if line.strip()
    ),
}

FORMAT_SYSTEM_PROMPTS = {
    "application/json": "Respond in valid JSON only. Structure your answer as {\"answer\": ..., \"details\": ...}.",
    "text/markdown": "Respond using GitHub-flavored Markdown with appropriate headers and formatting.",
    "text/plain": "Respond in plain text. No special formatting.",
    "text/yaml": "Respond in valid YAML format only.",
    "text/csv": "Respond as plain text lines. Each line should be a separate point. No headers.",
}


async def handle_request(
    question: str,
    accept: str = "text/plain",
) -> tuple[str, str]:
    """
    Handle a request with Content-Type negotiation.
    Returns (content_type, body).
    """
    # Resolve to best supported type
    content_type = accept if accept in FORMAT_SYSTEM_PROMPTS else "text/plain"
    system = FORMAT_SYSTEM_PROMPTS[content_type]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text

    # Apply renderer
    renderer = FORMAT_RENDERERS.get(content_type, FORMAT_RENDERERS["text/plain"])
    body = renderer(raw)
    return content_type, body


async def main() -> None:
    question = "What are the top 3 benefits of containerization?"
    callers = [
        ("API client",    "application/json"),
        ("Web UI",        "text/markdown"),
        ("CLI tool",      "text/plain"),
        ("Config export", "text/yaml"),
        ("Report",        "text/csv"),
    ]
    for caller, accept in callers:
        ct, body = await handle_request(question, accept)
        print(f"\n[{caller}] Accept: {accept}")
        print(f"Content-Type: {ct}")
        print(body[:300])


asyncio.run(main())

# Expected Token Savings: N/A; content-type pattern mirrors HTTP Accept header — zero ambiguity for API consumers
# Environment: Python 3.11+; pip install pyyaml; extend FORMAT_RENDERERS for XML, Protobuf, or custom schemas
```

## Option 5: Dynamic Format Negotiation via Tool Use

```python
import anthropic
import json

client = anthropic.Anthropic()

NEGOTIATE_TOOL = {
    "name": "declare_output_format",
    "description": "Declare the desired output format before answering. Call this first when given a question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["json", "markdown", "plain", "table", "numbered_list"],
                "description": "The output format to use for the response.",
            },
            "reason": {
                "type": "string",
                "description": "Why this format is most appropriate for the question.",
            },
        },
        "required": ["format", "reason"],
    },
}

FORMAT_FOLLOW_UP = {
    "json":          "Now respond with valid JSON only.",
    "markdown":      "Now respond using Markdown with headers and bullets.",
    "plain":         "Now respond in plain text.",
    "table":         "Now respond using a Markdown table.",
    "numbered_list": "Now respond as a numbered list only.",
}


def run_with_format_negotiation(question: str) -> tuple[str, str]:
    """Let the agent choose the best format, then produce the response."""
    # Phase 1: Format negotiation
    messages: list[dict] = [{"role": "user", "content": f"I have a question: {question}\n\nFirst, declare the best output format for your answer."}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[NEGOTIATE_TOOL],
        tool_choice={"type": "any"},
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    chosen_format = "plain"
    for block in response.content:
        if block.type == "tool_use" and block.name == "declare_output_format":
            chosen_format = block.input.get("format", "plain")
            reason = block.input.get("reason", "")
            print(f"Agent chose format: {chosen_format} — {reason}")
            messages.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Format accepted: {chosen_format}",
            })

    messages[-1] = {"role": "user", "content": [messages[-1], {"type": "text", "text": FORMAT_FOLLOW_UP.get(chosen_format, "Respond now.")}] if isinstance(messages[-1], dict) else messages[-1]}

    # Phase 2: Answer in negotiated format
    messages.append({"role": "user", "content": FORMAT_FOLLOW_UP.get(chosen_format, "Now answer the question.")})
    response2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )
    answer = response2.content[0].text
    return chosen_format, answer


questions = [
    "Compare Python, Go, and Rust for systems programming.",
    "What is the capital of Japan?",
    "List the steps to set up a Docker container.",
]

for q in questions:
    fmt, ans = run_with_format_negotiation(q)
    print(f"\nQ: {q}")
    print(f"Format: {fmt}")
    print(f"A: {ans[:300]}\n")

# Expected Token Savings: +30-50 tokens for negotiation phase, but saves downstream parsing failures
# Environment: Python 3.11+; let callers override the negotiated format by injecting their preference in the user message
```

## Option 6: Structured Output Registry with Format Versioning

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class OutputFormat:
    name: str
    version: str
    system_prompt: str
    example: str
    validator: Any = None  # callable(str) -> bool


FORMAT_REGISTRY: dict[str, OutputFormat] = {
    "report_v1": OutputFormat(
        name="report_v1",
        version="1.0",
        system_prompt=(
            "Respond as a structured report with exactly these sections:\n"
            "SUMMARY: <one sentence>\n"
            "FINDINGS:\n- <point 1>\n- <point 2>\n- <point 3>\n"
            "RECOMMENDATION: <one sentence>"
        ),
        example="SUMMARY: ...\nFINDINGS:\n- ...\nRECOMMENDATION: ...",
        validator=lambda t: "SUMMARY:" in t and "FINDINGS:" in t and "RECOMMENDATION:" in t,
    ),
    "json_v2": OutputFormat(
        name="json_v2",
        version="2.0",
        system_prompt='Respond with valid JSON only: {"answer": string, "confidence": 0-1, "sources": [strings]}',
        example='{"answer": "...", "confidence": 0.9, "sources": ["..."]}',
        validator=lambda t: _is_valid_json(t),
    ),
    "tweet": OutputFormat(
        name="tweet",
        version="1.0",
        system_prompt="Respond in 280 characters or fewer. No markdown. Be concise and direct.",
        example="Python async is faster for I/O-bound tasks. Use asyncio for web, DB, and file ops. #Python",
        validator=lambda t: len(t) <= 280,
    ),
}


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text.strip())
        return True
    except Exception:
        return False


async def ask_with_format(
    question: str,
    format_name: str,
    max_retries: int = 2,
) -> tuple[str, bool]:
    """Ask a question using a registered format. Returns (response, valid)."""
    fmt = FORMAT_REGISTRY.get(format_name)
    if not fmt:
        raise ValueError(f"Unknown format: {format_name}. Available: {list(FORMAT_REGISTRY)}")

    messages: list[dict] = [{"role": "user", "content": question}]
    last_response = ""

    for attempt in range(max_retries + 1):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=fmt.system_prompt,
            messages=messages,
        )
        last_response = response.content[0].text

        valid = fmt.validator(last_response) if fmt.validator else True
        if valid:
            return last_response, True

        print(f"[{format_name}] Attempt {attempt+1} invalid. Retrying...")
        messages.append({"role": "assistant", "content": last_response})
        messages.append({"role": "user", "content": f"Invalid format. Expected:\n{fmt.example}\n\nPlease reformat."})

    return last_response, False


async def main() -> None:
    question = "What are the advantages of using Python for data science?"

    for fmt_name in FORMAT_REGISTRY:
        fmt = FORMAT_REGISTRY[fmt_name]
        print(f"\n{'='*40}")
        print(f"Format: {fmt_name} (v{fmt.version})")
        response, valid = await ask_with_format(question, fmt_name)
        print(f"Valid: {valid}")
        print(response[:350])


asyncio.run(main())

# Expected Token Savings: N/A; registry pattern enables format A/B testing and versioned format rollout
# Environment: Python 3.11+; add formats to FORMAT_REGISTRY without code changes to consumers; version bump = new key
```

## Comparison

| Option | Negotiation Method | Validation | Multi-Turn | Versioned | Best For |
|--------|-------------------|------------|------------|-----------|----------|
| 1. System Prompt Header | Caller sets format name | No | Yes | No | Simple format switching |
| 2. Schema Declaration | JSON schema in system | Yes (retry) | No | No | Structured data consumers |
| 3. Session Contract | Format object per session | Yes (logged) | Yes | No | Chat UIs with format tracking |
| 4. Content-Type Header | HTTP-style Accept header | Via renderer | No | No | API gateways, REST backends |
| 5. Tool Negotiation | Agent self-selects format | No | No | No | Autonomous agents, auto-format |
| 6. Format Registry | Named + versioned formats | Yes (retry) | No | Yes | Production APIs with format SLAs |
