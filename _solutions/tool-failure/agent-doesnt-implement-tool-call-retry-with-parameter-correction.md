---
title: "Agent Doesn't Implement Tool Call Retry with Parameter Correction"
description: "When a tool call fails due to invalid parameters, agents should parse the error, correct the offending argument, and retry — not give up or retry with the same bad input."
difficulty: intermediate
category: tool-failure
tags: [tool-use, retry, parameter-correction, error-recovery, schema-validation, self-healing]
---

## Problem

An agent calls a tool with slightly wrong parameters — wrong type, missing required field, out-of-range value — and gets an error. Without parameter correction, the agent either fails the task entirely, asks the user to fix it, or blindly retries with the same invalid input and fails again. Smart retry logic parses the error message to understand what went wrong and corrects the specific parameter before retrying.

```python
# BAD: retry with identical parameters — guaranteed to fail again
async def call_tool_naive(name: str, params: dict) -> dict:
    for attempt in range(3):
        result = execute_tool(name, params)
        if result["success"]:
            return result
        # No correction — same params, same failure
    raise RuntimeError("Tool failed after 3 attempts")
```

## Solution 1: Error-Pattern-Based Parameter Correction

Parse common error patterns to identify and fix the offending parameter.

```python
import asyncio
import re
import json
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

# Simulated tool registry
def execute_tool(name: str, params: dict) -> dict:
    if name == "search_database":
        if not isinstance(params.get("limit"), int):
            return {"success": False, "error": "Parameter 'limit' must be an integer, got str"}
        if params.get("limit", 0) > 100:
            return {"success": False, "error": "Parameter 'limit' exceeds maximum value of 100"}
        if "query" not in params:
            return {"success": False, "error": "Missing required parameter: 'query'"}
        return {"success": True, "results": [f"Result for: {params['query']}"]}
    return {"success": False, "error": f"Unknown tool: {name}"}

ERROR_CORRECTIONS = [
    # Pattern: wrong type for a parameter
    (
        re.compile(r"Parameter '(\w+)' must be an? (\w+), got (\w+)"),
        lambda m, params: _correct_type(params, m.group(1), m.group(2))
    ),
    # Pattern: value exceeds maximum
    (
        re.compile(r"Parameter '(\w+)' exceeds maximum value of (\d+)"),
        lambda m, params: {**params, m.group(1): int(m.group(2))}
    ),
    # Pattern: value below minimum
    (
        re.compile(r"Parameter '(\w+)' below minimum value of (\d+)"),
        lambda m, params: {**params, m.group(1): int(m.group(2))}
    ),
    # Pattern: missing required parameter (can't auto-correct without default)
    (
        re.compile(r"Missing required parameter: '(\w+)'"),
        lambda m, params: params  # will be handled separately
    ),
]

def _correct_type(params: dict, param_name: str, target_type: str) -> dict:
    value = params.get(param_name)
    corrected = params.copy()
    try:
        if target_type == "integer" or target_type == "int":
            corrected[param_name] = int(value)
        elif target_type == "float" or target_type == "number":
            corrected[param_name] = float(value)
        elif target_type == "string" or target_type == "str":
            corrected[param_name] = str(value)
        elif target_type == "boolean" or target_type == "bool":
            corrected[param_name] = str(value).lower() in ("true", "1", "yes")
    except (ValueError, TypeError):
        pass
    return corrected

def auto_correct_params(error_msg: str, params: dict) -> dict | None:
    for pattern, corrector in ERROR_CORRECTIONS:
        m = pattern.search(error_msg)
        if m:
            corrected = corrector(m, params)
            if corrected != params:
                return corrected
    return None

async def call_tool_with_correction(
    tool_name: str,
    params: dict,
    max_retries: int = 3
) -> dict:
    current_params = params.copy()

    for attempt in range(max_retries):
        result = execute_tool(tool_name, current_params)

        if result["success"]:
            if attempt > 0:
                print(f"[Tool] Succeeded on attempt {attempt + 1} after parameter correction")
            return result

        error = result.get("error", "")
        print(f"[Tool] Attempt {attempt + 1} failed: {error}")

        corrected = auto_correct_params(error, current_params)
        if corrected:
            print(f"[Tool] Auto-corrected params: {current_params} -> {corrected}")
            current_params = corrected
        else:
            print(f"[Tool] Cannot auto-correct error: {error}")
            break

    return result

async def main():
    # Test: limit as string instead of int
    result = await call_tool_with_correction(
        "search_database",
        {"query": "python tutorials", "limit": "50"}  # "50" should be 50
    )
    print(f"Result: {result}")

    # Test: limit too high
    result = await call_tool_with_correction(
        "search_database",
        {"query": "AI news", "limit": 500}
    )
    print(f"Result: {result}")

asyncio.run(main())
```

## Solution 2: LLM-Assisted Parameter Correction

Use a small model to interpret the error and suggest corrected parameters.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CORRECTION_SYSTEM = """You are a tool parameter correction assistant. Given a tool name, its schema, the parameters that were used, and the error message received, output corrected parameters as valid JSON.

Rules:
- Output ONLY valid JSON matching the tool schema
- Fix only what caused the error
- Preserve all other parameters unchanged
- If you cannot determine the fix, output the original parameters unchanged"""

async def llm_correct_params(
    tool_name: str,
    tool_schema: dict,
    original_params: dict,
    error_message: str
) -> dict:
    prompt = f"""Tool: {tool_name}
Schema: {json.dumps(tool_schema, indent=2)}
Original parameters: {json.dumps(original_params, indent=2)}
Error: {error_message}

Output corrected parameters as JSON:"""

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=CORRECTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return original_params

# Simulated tool with schema
TOOLS = {
    "create_event": {
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "format": "YYYY-MM-DD"},
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 480},
                "attendees": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["title", "date", "duration_minutes"]
        }
    }
}

def execute_create_event(params: dict) -> dict:
    import re
    if not re.match(r"\d{4}-\d{2}-\d{2}", str(params.get("date", ""))):
        return {"success": False, "error": "date must be in YYYY-MM-DD format, got: " + str(params.get("date"))}
    duration = params.get("duration_minutes", 0)
    if not isinstance(duration, int):
        return {"success": False, "error": f"duration_minutes must be integer, got {type(duration).__name__}"}
    if duration > 480:
        return {"success": False, "error": "duration_minutes cannot exceed 480"}
    return {"success": True, "event_id": "evt-123", "params": params}

async def call_tool_with_llm_correction(
    tool_name: str,
    params: dict,
    max_retries: int = 3
) -> dict:
    current_params = params.copy()
    schema = TOOLS.get(tool_name, {}).get("schema", {})

    for attempt in range(max_retries):
        if tool_name == "create_event":
            result = execute_create_event(current_params)
        else:
            result = {"success": False, "error": "Unknown tool"}

        if result["success"]:
            return result

        error = result.get("error", "")
        print(f"[Attempt {attempt + 1}] Error: {error}")

        if attempt < max_retries - 1:
            corrected = await llm_correct_params(tool_name, schema, current_params, error)
            if corrected != current_params:
                print(f"[LLM Correction] {current_params} -> {corrected}")
                current_params = corrected
            else:
                print("[LLM Correction] Could not determine fix")
                break

    return result

async def main():
    # Wrong date format
    result = await call_tool_with_llm_correction(
        "create_event",
        {"title": "Team Standup", "date": "April 20, 2025", "duration_minutes": 30}
    )
    print(f"Final: {result}")

asyncio.run(main())
```

## Solution 3: Schema-Driven Coercion Before Calling

Validate and coerce parameters against the JSON schema before the first call.

```python
import asyncio
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

def coerce_value(value: Any, schema: dict) -> Any:
    """Attempt to coerce a value to match its schema type."""
    expected_type = schema.get("type")
    if value is None:
        return schema.get("default")

    try:
        if expected_type == "integer":
            return int(float(str(value)))
        elif expected_type == "number":
            return float(str(value))
        elif expected_type == "string":
            return str(value)
        elif expected_type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1", "yes", "on")
        elif expected_type == "array" and not isinstance(value, list):
            # Single value -> wrap in list
            return [value]
    except (ValueError, TypeError):
        pass
    return value

def coerce_params(params: dict, schema: dict) -> tuple[dict, list[str]]:
    """
    Coerce params to match schema. Returns (coerced_params, warnings).
    """
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    coerced = {}
    warnings = []

    for key, prop_schema in properties.items():
        if key in params:
            original = params[key]
            coerced_val = coerce_value(original, prop_schema)
            coerced[key] = coerced_val
            if coerced_val != original:
                warnings.append(f"Coerced '{key}': {original!r} -> {coerced_val!r}")

            # Range checks
            if "minimum" in prop_schema and isinstance(coerced_val, (int, float)):
                if coerced_val < prop_schema["minimum"]:
                    coerced[key] = prop_schema["minimum"]
                    warnings.append(f"Clamped '{key}' to minimum {prop_schema['minimum']}")
            if "maximum" in prop_schema and isinstance(coerced_val, (int, float)):
                if coerced_val > prop_schema["maximum"]:
                    coerced[key] = prop_schema["maximum"]
                    warnings.append(f"Clamped '{key}' to maximum {prop_schema['maximum']}")

        elif key in required:
            default = prop_schema.get("default")
            if default is not None:
                coerced[key] = default
                warnings.append(f"Filled required '{key}' with default: {default!r}")

    # Preserve extra params not in schema
    for key in params:
        if key not in coerced:
            coerced[key] = params[key]

    return coerced, warnings

TOOL_SCHEMAS = {
    "send_notification": {
        "properties": {
            "user_id": {"type": "string"},
            "message": {"type": "string"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            "channels": {"type": "array", "items": {"type": "string"}, "default": ["email"]}
        },
        "required": ["user_id", "message"]
    }
}

def execute_send_notification(params: dict) -> dict:
    # Strict executor — no coercion here
    if not isinstance(params.get("priority"), int):
        return {"success": False, "error": "priority must be int"}
    if not isinstance(params.get("channels"), list):
        return {"success": False, "error": "channels must be array"}
    return {"success": True, "sent": True, "params": params}

async def call_with_schema_coercion(tool_name: str, params: dict) -> dict:
    schema = TOOL_SCHEMAS.get(tool_name, {})
    coerced, warnings = coerce_params(params, schema)

    if warnings:
        print(f"[Coercion] {tool_name}: {warnings}")

    result = execute_send_notification(coerced)
    if not result["success"]:
        # Still failed after coercion — log and return
        print(f"[Error] Tool failed even after coercion: {result['error']}")
    return result

async def main():
    result = await call_with_schema_coercion(
        "send_notification",
        {
            "user_id": 12345,          # int, needs to be str
            "message": "Hello!",
            "priority": "2",           # str, needs to be int
            "channels": "slack"        # str, needs to be array
        }
    )
    print(f"Result: {result}")

asyncio.run(main())
```

## Solution 4: Multi-Turn Tool Error Recovery in Agent Loop

Integrate parameter correction into the full agent tool-use loop.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

TOOLS = [
    {
        "name": "calculate_statistics",
        "description": "Calculate statistics for a list of numbers",
        "input_schema": {
            "type": "object",
            "properties": {
                "numbers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "List of numbers to analyze"
                },
                "operations": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mean", "median", "std", "min", "max"]},
                    "description": "Statistical operations to perform"
                }
            },
            "required": ["numbers", "operations"]
        }
    }
]

def execute_calculate_statistics(params: dict) -> str:
    numbers = params.get("numbers", [])
    operations = params.get("operations", [])

    if not isinstance(numbers, list):
        return json.dumps({"error": "numbers must be an array, not " + type(numbers).__name__})
    if not all(isinstance(n, (int, float)) for n in numbers):
        return json.dumps({"error": "all elements in numbers must be numeric"})
    if not numbers:
        return json.dumps({"error": "numbers array cannot be empty"})

    import statistics
    results = {}
    for op in operations:
        if op == "mean":
            results["mean"] = statistics.mean(numbers)
        elif op == "median":
            results["median"] = statistics.median(numbers)
        elif op == "std":
            results["std"] = statistics.stdev(numbers) if len(numbers) > 1 else 0
        elif op == "min":
            results["min"] = min(numbers)
        elif op == "max":
            results["max"] = max(numbers)
    return json.dumps(results)

def dispatch_tool(name: str, params: dict) -> str:
    if name == "calculate_statistics":
        return execute_calculate_statistics(params)
    return json.dumps({"error": f"Unknown tool: {name}"})

async def agent_loop_with_correction(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    tool_error_counts: dict[str, int] = {}
    max_tool_errors = 2

    for _ in range(10):  # max iterations
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages
        )

        # Collect text blocks
        text_parts = [b.text for b in response.content if hasattr(b, "text")]

        if response.stop_reason == "end_turn":
            return " ".join(text_parts) or "Done."

        if response.stop_reason != "tool_use":
            return " ".join(text_parts) or "Unexpected stop reason."

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_id = block.id
            error_key = f"{tool_id}"

            raw_result = dispatch_tool(tool_name, tool_input)
            result_data = json.loads(raw_result)

            if "error" in result_data:
                error_count = tool_error_counts.get(error_key, 0) + 1
                tool_error_counts[error_key] = error_count

                error_msg = result_data["error"]
                if error_count <= max_tool_errors:
                    # Return error to model so it can correct
                    correction_hint = (
                        f"Tool error: {error_msg}\n"
                        f"Please correct your tool call parameters and try again. "
                        f"Attempt {error_count}/{max_tool_errors}."
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": correction_hint,
                        "is_error": True
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": f"Tool failed after {max_tool_errors} correction attempts: {error_msg}",
                        "is_error": True
                    })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": raw_result
                })

        messages.append({"role": "user", "content": tool_results})

    return "Max iterations reached."

async def main():
    result = await agent_loop_with_correction(
        "Calculate the mean and max of these numbers: 10, 20, 30, 40, 50"
    )
    print(result)

asyncio.run(main())
```

## Solution 5: Typed Parameter Builder with Validation

Build parameters through a typed interface that catches errors before calling.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field, asdict
from typing import Any
import json

client = AsyncAnthropic()

@dataclass
class ToolParameterBuilder:
    """Type-safe parameter builder with auto-correction."""
    _params: dict = field(default_factory=dict)
    _errors: list[str] = field(default_factory=list)
    _corrections: list[str] = field(default_factory=list)

    def set_string(self, key: str, value: Any, max_length: int | None = None) -> "ToolParameterBuilder":
        str_val = str(value) if not isinstance(value, str) else value
        if not isinstance(value, str):
            self._corrections.append(f"Coerced '{key}' to string")
        if max_length and len(str_val) > max_length:
            str_val = str_val[:max_length]
            self._corrections.append(f"Truncated '{key}' to {max_length} chars")
        self._params[key] = str_val
        return self

    def set_int(self, key: str, value: Any, min_val: int | None = None, max_val: int | None = None) -> "ToolParameterBuilder":
        try:
            int_val = int(float(str(value)))
        except (ValueError, TypeError):
            self._errors.append(f"Cannot convert '{key}' value {value!r} to integer")
            return self
        if not isinstance(value, int):
            self._corrections.append(f"Coerced '{key}' to int")
        if min_val is not None and int_val < min_val:
            int_val = min_val
            self._corrections.append(f"Clamped '{key}' to min={min_val}")
        if max_val is not None and int_val > max_val:
            int_val = max_val
            self._corrections.append(f"Clamped '{key}' to max={max_val}")
        self._params[key] = int_val
        return self

    def set_list(self, key: str, value: Any, item_type: type = str) -> "ToolParameterBuilder":
        if not isinstance(value, list):
            value = [value]
            self._corrections.append(f"Wrapped '{key}' in array")
        coerced = []
        for item in value:
            try:
                coerced.append(item_type(item))
            except (ValueError, TypeError):
                self._corrections.append(f"Skipped invalid item in '{key}': {item!r}")
        self._params[key] = coerced
        return self

    def build(self) -> tuple[dict, list[str], list[str]]:
        return self._params.copy(), self._corrections.copy(), self._errors.copy()

def execute_tool_strict(name: str, params: dict) -> dict:
    if name == "send_email":
        required = ["to", "subject", "body"]
        for r in required:
            if r not in params:
                return {"success": False, "error": f"Missing required: {r}"}
        if not isinstance(params.get("max_retries"), int):
            return {"success": False, "error": "max_retries must be int"}
        return {"success": True, "message_id": "msg-001"}
    return {"success": False, "error": "Unknown tool"}

async def call_with_typed_builder(raw_params: dict) -> dict:
    builder = ToolParameterBuilder()
    (
        builder
        .set_string("to", raw_params.get("to", ""), max_length=254)
        .set_string("subject", raw_params.get("subject", ""))
        .set_string("body", raw_params.get("body", ""))
        .set_int("max_retries", raw_params.get("max_retries", 3), min_val=0, max_val=10)
        .set_list("tags", raw_params.get("tags", []), str)
    )

    params, corrections, errors = builder.build()

    if errors:
        return {"success": False, "error": f"Parameter errors: {errors}"}
    if corrections:
        print(f"[Builder] Applied corrections: {corrections}")

    return execute_tool_strict("send_email", params)

async def main():
    result = await call_with_typed_builder({
        "to": "user@example.com",
        "subject": "Hello",
        "body": "Test message",
        "max_retries": "5",   # str, should be int
        "tags": "important"   # str, should be list
    })
    print(f"Result: {result}")

asyncio.run(main())
```

## Solution 6: Diff-Based Correction with Change Tracking

Track what was corrected between attempts for observability and learning.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class CorrectionRecord:
    attempt: int
    original_params: dict
    corrected_params: dict
    error_that_triggered: str
    correction_method: str

    def diff(self) -> dict:
        diffs = {}
        all_keys = set(self.original_params) | set(self.corrected_params)
        for k in all_keys:
            orig = self.original_params.get(k)
            corr = self.corrected_params.get(k)
            if orig != corr:
                diffs[k] = {"from": orig, "to": corr}
        return diffs

def rule_based_correct(params: dict, error: str) -> tuple[dict, str] | None:
    """Returns (corrected_params, correction_method) or None."""
    import re
    corrected = params.copy()

    # Rule: type mismatch
    m = re.search(r"'(\w+)' must be (\w+)", error)
    if m:
        param, target = m.group(1), m.group(2)
        if param in corrected:
            try:
                if target in ("int", "integer"):
                    corrected[param] = int(float(str(corrected[param])))
                elif target in ("float", "number"):
                    corrected[param] = float(str(corrected[param]))
                elif target in ("str", "string"):
                    corrected[param] = str(corrected[param])
                return corrected, f"type_coercion:{param}:{target}"
            except (ValueError, TypeError):
                pass

    # Rule: value too large
    m = re.search(r"'(\w+)' (?:cannot exceed|maximum is) (\d+)", error)
    if m:
        param, max_val = m.group(1), int(m.group(2))
        if param in corrected:
            corrected[param] = max_val
            return corrected, f"clamp_max:{param}:{max_val}"

    # Rule: unknown enum value
    m = re.search(r"'(\w+)' must be one of: (.+)", error)
    if m:
        param, options_str = m.group(1), m.group(2)
        options = [o.strip().strip("'\"") for o in options_str.split(",")]
        if param in corrected and options:
            corrected[param] = options[0]  # Use first valid option
            return corrected, f"enum_fallback:{param}:{options[0]}"

    return None

def execute_api_call(params: dict) -> dict:
    if not isinstance(params.get("timeout"), int):
        return {"success": False, "error": "'timeout' must be int"}
    if params.get("timeout", 0) > 300:
        return {"success": False, "error": "'timeout' cannot exceed 300"}
    if params.get("method") not in ("GET", "POST", "PUT", "DELETE"):
        return {"success": False, "error": "'method' must be one of: 'GET', 'POST', 'PUT', 'DELETE'"}
    return {"success": True, "status": 200}

async def call_with_correction_tracking(
    tool_name: str,
    params: dict,
    max_retries: int = 4
) -> tuple[dict, list[CorrectionRecord]]:
    current_params = params.copy()
    correction_history: list[CorrectionRecord] = []

    for attempt in range(max_retries):
        result = execute_api_call(current_params)

        if result["success"]:
            if correction_history:
                print(f"[Recovery] Succeeded after {len(correction_history)} corrections")
                for r in correction_history:
                    print(f"  Attempt {r.attempt}: {r.correction_method} — diff: {r.diff()}")
            return result, correction_history

        error = result.get("error", "")
        print(f"[Attempt {attempt + 1}] Error: {error}")

        correction = rule_based_correct(current_params, error)
        if correction:
            corrected_params, method = correction
            record = CorrectionRecord(
                attempt=attempt + 1,
                original_params=current_params.copy(),
                corrected_params=corrected_params.copy(),
                error_that_triggered=error,
                correction_method=method
            )
            correction_history.append(record)
            current_params = corrected_params
        else:
            print(f"[Correction] No rule matched for: {error}")
            break

    return result, correction_history

async def main():
    result, history = await call_with_correction_tracking(
        "http_request",
        {"url": "https://api.example.com", "method": "get", "timeout": "600"}
    )
    print(f"Final result: {result}")
    print(f"Corrections applied: {len(history)}")

asyncio.run(main())
```

## Comparison

| Approach | Speed | Auto-Corrects | Handles | Best For |
|---|---|---|---|---|
| Pattern-Based Correction | Fast | Type, range, missing | Common schema errors | Production tools |
| LLM-Assisted Correction | Slow | Anything describable | Complex/unusual errors | Varied tool schemas |
| Schema-Driven Coercion | Fast | Type, range, defaults | Structured schema | Well-typed APIs |
| Agent Loop Recovery | Medium | Anything model can fix | Full agent context | Multi-step agents |
| Typed Builder | Fast | Type, range, wrapping | Compile-time patterns | Strongly typed tools |
| Diff Tracking | Fast | Pattern-based | Auditable corrections | Debugging, observability |

**Rule of thumb**: Apply schema-driven coercion first (free, no API calls), then pattern-based correction on errors, and use LLM-assisted correction only when patterns fail — it adds ~200ms latency but handles any describable error.
