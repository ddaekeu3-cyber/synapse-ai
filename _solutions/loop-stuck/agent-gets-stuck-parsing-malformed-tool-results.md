---
layout: solution
title: "Agent gets stuck parsing malformed tool results"
category: loop-stuck
description: "Agent expects tool results in a specific format (JSON, XML, structured text) but the tool returns malformed, truncated, or unexpected output. The agent retries the same parse repeatedly, each time failing and generating the same next tool call — stuck in a parse-fail-retry loop until it hits the turn limit."
tags: [loop-stuck, tool-use, parsing, error-handling, json, resilience, validation]
---

## Symptom

The agent calls `get_user_data()` expecting JSON like `{"user_id": 123, "name": "Alice"}`. The tool returns `{"user_id": 123, "name": "Ali` (truncated). The agent tries to parse it, fails, asks the model "the result seems incomplete, retry", gets the same truncated result, fails again — repeating 10 times before hitting the turn limit. No useful work is done and the task fails entirely.

## Root Cause

The agent's tool result handling assumes well-formed output. When parsing fails, the error recovery path calls the same tool again with the same arguments, producing the same bad result. There is no circuit breaker on repeated identical tool calls, no fallback for unparseable results, and no detection of "this tool is consistently broken."

## Fix

Detect parse failures and apply a graduated response: (1) retry with explicit format instructions, (2) attempt partial extraction from malformed output, (3) fall back to a degraded response without the tool data, (4) circuit-break after N consecutive parse failures for the same tool.

---

### Option 1 — Graceful parse failure with structured error injection

```python
import anthropic
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def try_parse_json(raw: str) -> tuple[dict | None, str]:
    """
    Try to parse JSON, returning (parsed, error_description).
    Returns (None, error) on failure rather than raising.
    """
    raw = raw.strip()

    # Attempt 1: direct parse
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass

    # Attempt 2: find JSON-like substring
    match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0)), "extracted from malformed response"
        except json.JSONDecodeError:
            pass

    # Attempt 3: extract key-value pairs manually
    pairs = re.findall(r'"(\w+)"\s*:\s*"([^"]*)"', raw)
    if pairs:
        return dict(pairs), "extracted from partial JSON"

    # Complete parse failure
    return None, f"Could not parse JSON: {raw[:100]!r}"


def safe_tool_result(tool_name: str, raw_output: str) -> str:
    """
    Process a tool result, handling malformed output gracefully.
    Returns a string safe to inject into the model's context.
    """
    parsed, error = try_parse_json(raw_output)

    if parsed is not None:
        if error:
            print(f"[Parser:{tool_name}] Partial parse — {error}")
        return json.dumps(parsed)

    # Complete failure — inject a structured error so the model can react
    print(f"[Parser:{tool_name}] Parse failed — injecting error message")
    return json.dumps({
        "error": "parse_failed",
        "tool": tool_name,
        "raw_preview": raw_output[:200],
        "message": (
            f"The {tool_name} tool returned malformed data that could not be parsed. "
            "Please proceed with partial information or ask for clarification."
        ),
    })


TOOLS = [
    {
        "name": "get_user_data",
        "description": "Get user profile data by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
]


def simulate_broken_tool(user_id: str) -> str:
    """Simulate a tool that returns truncated JSON."""
    return '{"user_id": "123", "name": "Ali'   # truncated!


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for turn in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    raw = simulate_broken_tool(block.input.get("user_id", ""))
                    # Safe injection — model sees structured error, not confusing garbage
                    safe_result = safe_tool_result(block.name, raw)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": safe_result,
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


result = run_agent("What can you tell me about user 123?")
print(result)
# Model receives structured error and gracefully responds without getting stuck
```

**Expected Token Savings:** Structured error injection ends the parse-retry loop immediately — instead of 10 failed retries (~3000 tokens), the model receives one clear error and produces a graceful answer on the next turn; saves ~2700 tokens per stuck loop.
**Environment:** Any agent calling tools that can return malformed output; structured error injection is the minimal defensive layer that costs zero tokens over the failed-parse path.

---

### Option 2 — Retry with format clarification on parse failure

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_PARSE_RETRIES = 2


def call_tool_with_format_retry(
    tool_name: str,
    tool_id: str,
    tool_input: dict,
    execute_fn,
    expected_schema: dict,
) -> str:
    """
    Call a tool, and if parsing fails, retry with an explicit format instruction.
    Returns a JSON string — either the real result or a structured error.
    """
    schema_desc = json.dumps(expected_schema, indent=2)

    for attempt in range(MAX_PARSE_RETRIES + 1):
        raw = execute_fn(**tool_input)

        try:
            parsed = json.loads(raw.strip())
            if attempt > 0:
                print(f"[FormatRetry:{tool_name}] Succeeded on attempt {attempt+1}")
            return json.dumps(parsed)

        except json.JSONDecodeError:
            if attempt < MAX_PARSE_RETRIES:
                print(
                    f"[FormatRetry:{tool_name}] Parse failed attempt {attempt+1} "
                    f"— retrying with format hint"
                )
                # Re-execute with explicit format requirements
                # (In a real system, you'd modify the tool call arguments to
                #  include a format reminder or use a wrapper endpoint)
                tool_input = {**tool_input, "_format_hint": schema_desc}
            else:
                print(f"[FormatRetry:{tool_name}] All {MAX_PARSE_RETRIES+1} attempts failed")
                return json.dumps({
                    "error": "persistent_parse_failure",
                    "tool": tool_name,
                    "attempts": attempt + 1,
                    "raw_preview": raw[:100],
                    "message": (
                        f"Tool returned unparseable output after {attempt+1} attempts. "
                        "Use available information to respond without this data."
                    ),
                })

    return json.dumps({"error": "unexpected_state"})


TOOLS = [
    {
        "name": "fetch_config",
        "description": "Fetch application configuration.",
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
        },
    },
]

EXPECTED_SCHEMAS = {
    "fetch_config": {"app_id": "string", "settings": {"key": "value"}, "version": "string"},
}

_attempt_count = {}


def flaky_fetch_config(app_id: str, _format_hint: str = "") -> str:
    """Simulates a tool that eventually returns valid JSON after a hint."""
    key = f"fetch_config_{app_id}"
    _attempt_count[key] = _attempt_count.get(key, 0) + 1
    if _attempt_count[key] < 2:
        return '{"app_id": "app1", "settings": {"theme": "dark"'   # truncated
    return '{"app_id": "app1", "settings": {"theme": "dark"}, "version": "2.1"}'


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    schema = EXPECTED_SCHEMAS.get(block.name, {})
                    result = call_tool_with_format_retry(
                        block.name, block.id, block.input,
                        flaky_fetch_config, schema,
                    )
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


print(run_agent("What's the config for app1?"))
```

**Expected Token Savings:** Format-hint retry resolves transient parse failures (tool implementation bug, encoding issue) in 1–2 extra calls (~400 tokens) vs 10+ identical retries (~3000 tokens) in the stuck loop; persistent failures cap at 2 retries and exit cleanly.
**Environment:** Agents calling third-party APIs or tools with intermittent formatting bugs; the retry-with-hint pattern is especially effective when the tool can be nudged to return valid JSON.

---

### Option 3 — Tool result fingerprinting to detect repeat failures

```python
import anthropic
import json
import hashlib
from collections import defaultdict

client = anthropic.Anthropic(api_key="sk-live-...")


class ToolCallTracker:
    """
    Tracks tool call results by fingerprint to detect when the same
    bad result is being returned repeatedly — indicating a stuck loop.
    """
    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats
        # tool_name → {result_hash: count}
        self._result_counts: dict[str, dict[str, int]] = defaultdict(dict)
        self._stuck_tools: set[str] = set()

    def fingerprint(self, result: str) -> str:
        """Create a hash of the first 200 chars of the result."""
        return hashlib.md5(result[:200].encode()).hexdigest()[:8]

    def record(self, tool_name: str, result: str) -> bool:
        """
        Record a tool result. Returns True if the tool appears stuck
        (same result returned too many times).
        """
        if tool_name in self._stuck_tools:
            return True

        fp = self.fingerprint(result)
        counts = self._result_counts[tool_name]
        counts[fp] = counts.get(fp, 0) + 1

        if counts[fp] > self.max_repeats:
            self._stuck_tools.add(tool_name)
            print(
                f"[Tracker:{tool_name}] STUCK — same result returned "
                f"{counts[fp]} times (fp={fp})"
            )
            return True

        return False

    def is_stuck(self, tool_name: str) -> bool:
        return tool_name in self._stuck_tools

    def reset(self, tool_name: str):
        self._stuck_tools.discard(tool_name)
        self._result_counts[tool_name].clear()


tracker = ToolCallTracker(max_repeats=2)

TOOLS = [
    {
        "name": "get_inventory",
        "description": "Get inventory data.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
]


def broken_inventory(product_id: str) -> str:
    """Always returns the same malformed JSON."""
    return '{"product_id": "' + product_id + '", "stock":, "price":'  # malformed


STUCK_TOOL_SYSTEM_INSTRUCTION = (
    "The {tool} tool is currently unavailable due to data format issues. "
    "Please proceed with your response using available information only. "
    "Do not attempt to call {tool} again in this conversation."
)


def run_agent_with_tracking(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    system = "You are a helpful inventory assistant."

    for turn in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            newly_stuck = []

            for block in response.content:
                if block.type == "tool_use":
                    if tracker.is_stuck(block.name):
                        # Skip stuck tool — inject unavailable message
                        content = json.dumps({
                            "error": "tool_unavailable",
                            "message": f"{block.name} is unavailable. Do not retry.",
                        })
                    else:
                        raw = broken_inventory(block.input.get("product_id", ""))
                        is_stuck = tracker.record(block.name, raw)

                        if is_stuck:
                            newly_stuck.append(block.name)
                            content = json.dumps({
                                "error": "repeated_failure",
                                "tool": block.name,
                                "message": (
                                    f"{block.name} consistently returns malformed data. "
                                    "Respond without this information."
                                ),
                            })
                        else:
                            content = raw   # pass through (even if malformed)

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })

            # Update system prompt to prevent further calls to stuck tools
            if newly_stuck:
                system += "\n" + " ".join(
                    STUCK_TOOL_SYSTEM_INSTRUCTION.format(tool=t) for t in newly_stuck
                )

            messages.append({"role": "user", "content": results})

    return "Max turns reached"


print(run_agent_with_tracking("What's the stock level for product SKU-123?"))
```

**Expected Token Savings:** Fingerprint detection trips after 2 identical bad results (instead of 10+) — saves 8 × (tool_call + tool_result) tokens ≈ 800 tokens per stuck loop; the updated system prompt prevents the model from re-calling the stuck tool in subsequent turns.
**Environment:** Agents calling unreliable tools; fingerprinting catches both malformed results and any other "stuck" pattern where the same response is returned repeatedly.

---

### Option 4 — Schema-validated tool results with partial extraction

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_partial(raw: str, required_fields: list[str]) -> dict | None:
    """
    Try to extract specific fields from a malformed JSON string.
    Used when full parse fails but partial data is recoverable.
    """
    import re
    extracted = {}
    for field in required_fields:
        # Try to find "field": value in the raw string
        patterns = [
            rf'"{field}"\s*:\s*"([^"]*)"',   # string value
            rf'"{field}"\s*:\s*(\d+(?:\.\d+)?)',  # numeric value
            rf'"{field}"\s*:\s*(true|false)',  # boolean value
        ]
        for pattern in patterns:
            match = re.search(pattern, raw)
            if match:
                val = match.group(1)
                # Type coercion
                if val in ("true", "false"):
                    val = val == "true"
                elif re.match(r'^\d+$', val):
                    val = int(val)
                elif re.match(r'^\d+\.\d+$', val):
                    val = float(val)
                extracted[field] = val
                break

    return extracted if extracted else None


def validate_and_extract(
    tool_name: str,
    raw_result: str,
    required_fields: list[str],
    optional_fields: list[str] | None = None,
) -> tuple[dict, str]:
    """
    Validate tool result against schema.
    Returns (data, quality) where quality is 'full', 'partial', or 'empty'.
    """
    # Try full parse first
    try:
        data = json.loads(raw_result.strip())
        missing_required = [f for f in required_fields if f not in data]
        if not missing_required:
            return data, "full"
        print(f"[Schema:{tool_name}] Missing required fields: {missing_required}")
        return data, "partial"
    except json.JSONDecodeError:
        pass

    # Fall back to partial extraction
    all_fields = required_fields + (optional_fields or [])
    partial = extract_partial(raw_result, all_fields)
    if partial:
        print(f"[Schema:{tool_name}] Partial extraction: found {list(partial.keys())}")
        return partial, "partial"

    print(f"[Schema:{tool_name}] No data recoverable")
    return {}, "empty"


TOOLS = [
    {
        "name": "get_order",
        "description": "Get order details by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]

TOOL_SCHEMAS = {
    "get_order": {
        "required": ["order_id", "status", "total"],
        "optional": ["customer_name", "items", "shipping_address"],
    },
}


def run_agent_schema_validated(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Simulate malformed tool result
                    raw = '{"order_id": "ORD-456", "status": "shipped", "total":'  # truncated

                    schema = TOOL_SCHEMAS.get(block.name, {"required": [], "optional": []})
                    data, quality = validate_and_extract(
                        block.name, raw,
                        schema["required"],
                        schema.get("optional"),
                    )

                    if quality == "empty":
                        content = json.dumps({
                            "error": "no_data_recoverable",
                            "tool": block.name,
                            "message": "Tool returned unreadable data. Proceed without it.",
                        })
                    else:
                        content = json.dumps({
                            **data,
                            "_data_quality": quality,
                            "_missing_fields": [f for f in schema["required"] if f not in data],
                        })

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


print(run_agent_schema_validated("What's the status of order ORD-456?"))
```

**Expected Token Savings:** Partial extraction recovers usable data from truncated results — for a truncated response where 3/4 of required fields are present, the agent can still give a useful answer without retrying; saves 1–3 retry turns (~600–1800 tokens) when partial data is sufficient.
**Environment:** Agents with well-defined tool output schemas; the schema-aware extractor is more robust than a generic JSON parser for domain-specific APIs with known field names.

---

### Option 5 — Parse failure circuit breaker with exponential backoff

```python
import anthropic
import json
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class ParseCircuitBreaker:
    """
    Circuit breaker specifically for parse failures.
    Trips after N consecutive parse failures; recovers after a backoff period.
    """
    tool_name: str
    failure_threshold: int = 3
    backoff_seconds: float = 10.0
    _consecutive_failures: int = 0
    _tripped_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._tripped_at is None:
            return False
        if time.monotonic() - self._tripped_at >= self.backoff_seconds:
            print(f"[ParseCB:{self.tool_name}] Attempting recovery after backoff")
            self._tripped_at = None
            self._consecutive_failures = 0
            return False
        return True

    def record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            if self._tripped_at is None:
                self._tripped_at = time.monotonic()
                print(
                    f"[ParseCB:{self.tool_name}] TRIPPED after "
                    f"{self._consecutive_failures} parse failures"
                )

    def record_success(self):
        self._consecutive_failures = 0
        self._tripped_at = None


_breakers: dict[str, ParseCircuitBreaker] = {}


def get_breaker(tool_name: str) -> ParseCircuitBreaker:
    if tool_name not in _breakers:
        _breakers[tool_name] = ParseCircuitBreaker(tool_name)
    return _breakers[tool_name]


async def safe_tool_call(tool_name: str, tool_id: str, execute_coro) -> str:
    breaker = get_breaker(tool_name)

    if breaker.is_open:
        return json.dumps({
            "error": "circuit_open",
            "tool": tool_name,
            "message": (
                f"{tool_name} is temporarily unavailable due to repeated parse failures. "
                "Please proceed without this data."
            ),
        })

    try:
        raw_result = await execute_coro
    except Exception as e:
        breaker.record_failure()
        return json.dumps({"error": "execution_failed", "detail": str(e)})

    # Attempt to parse
    try:
        json.loads(raw_result.strip())
        breaker.record_success()
        return raw_result
    except (json.JSONDecodeError, AttributeError):
        breaker.record_failure()
        return json.dumps({
            "error": "parse_failed",
            "tool": tool_name,
            "raw_preview": str(raw_result)[:100],
            "circuit_state": "open" if breaker.is_open else "closed",
            "message": "Tool returned unparseable data.",
        })


TOOLS = [
    {
        "name": "api_call",
        "description": "Call the external API.",
        "input_schema": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
        },
    },
]


async def broken_api_call(endpoint: str) -> str:
    await asyncio.sleep(0.1)
    return "not json at all"


async def run_async_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await safe_tool_call(
                        block.name, block.id,
                        broken_api_call(block.input.get("endpoint", "")),
                    )
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


asyncio.run(run_async_agent("Call the /status endpoint and report the results"))
```

**Expected Token Savings:** Circuit breaker trips after 3 failures instead of 10+ — saves 7 × ~400 tokens = 2800 tokens per stuck loop; the backoff period prevents the tool from being retried again immediately, giving it time to recover.
**Environment:** Agents with external API tools that experience intermittent data format issues; the circuit breaker pattern is appropriate when the tool failure is expected to be temporary.

---

### Option 6 — Fuzzy result extraction with model-assisted recovery

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

EXTRACTION_SYSTEM = (
    "You are a data extraction specialist. "
    "Given malformed or partial data, extract as much usable information as possible "
    "and return it as valid JSON. "
    "If a field appears partially (e.g. truncated), include what's available with a "
    "'_truncated': true flag on that field. "
    "Never invent data — only extract what's explicitly present."
)


def model_assisted_extraction(
    tool_name: str,
    raw_result: str,
    expected_fields: list[str],
) -> dict:
    """
    Use Haiku to extract data from malformed tool results.
    More powerful than regex but costs ~30 tokens — only use when JSON parsing fails.
    """
    try:
        return json.loads(raw_result.strip())   # free path first
    except json.JSONDecodeError:
        pass

    # Fall back to Haiku extraction
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=EXTRACTION_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Extract these fields from the data: {expected_fields}\n\n"
                f"Raw data: {raw_result[:1000]}"
            ),
        }],
    )

    try:
        extracted = json.loads(response.content[0].text.strip())
        print(f"[ModelExtract:{tool_name}] Recovered {list(extracted.keys())} from malformed data")
        return {"_source": "model_extracted", **extracted}
    except json.JSONDecodeError:
        print(f"[ModelExtract:{tool_name}] Extraction failed — using empty result")
        return {
            "_error": "extraction_failed",
            "_raw_preview": raw_result[:100],
            "_message": f"Could not extract data from {tool_name} result.",
        }


TOOLS = [
    {
        "name": "get_product",
        "description": "Get product details.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
]

TOOL_EXPECTED_FIELDS = {
    "get_product": ["sku", "name", "price", "in_stock"],
}


def run_agent_fuzzy_extract(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    raw = '{"sku": "XYZ-789", "name": "Widget Pro", "price":'  # truncated

                    fields = TOOL_EXPECTED_FIELDS.get(block.name, [])
                    extracted = model_assisted_extraction(block.name, raw, fields)

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(extracted),
                    })
            messages.append({"role": "user", "content": results})

    # Comparison table
    # | Option | Failure Handling | LLM Cost | Partial Recovery |
    # |--------|-----------------|---------|-----------------|
    # | 1 Structured error | Inject error JSON | None | No |
    # | 2 Format-hint retry | Retry with hint | ~0 extra | Sometimes |
    # | 3 Fingerprint tracker | Circuit break | None | No |
    # | 4 Schema extraction | Regex field extract | None | Yes |
    # | 5 Parse circuit breaker | Backoff + open | None | No |
    # | 6 Model-assisted | Haiku extraction | ~30 tok | Yes (best) |

    return "Max turns reached"


print(run_agent_fuzzy_extract("Tell me about product SKU XYZ-789"))
```

**Expected Token Savings:** Haiku extraction costs ~30 tokens per malformed result; recovers partial data that would otherwise require 1–3 extra tool call retries (~600–1800 tokens); net positive when partial recovery allows the agent to proceed without retry.
**Environment:** Agents where partial data is significantly better than no data; the Haiku extraction is more flexible than regex and handles irregular formats that pattern matching would miss.
