---
layout: solution
title: "Agent Ignores Tool Response Errors — Continues as If Tool Succeeded"
category: tool-failure
description: "Tool returns {\"error\": \"permission denied\"} or {\"success\": false}. Agent reads the response, says 'the file was written successfully', and proceeds to the next step based on a false assumption. The error was in the tool result — but the model didn't treat it as an error."
tags: [tool-failure, error-handling, tool-use, validation, error-detection, defensive-coding, anthropic, agentic]
---

# Agent Ignores Tool Response Errors — Continues as If Tool Succeeded

## Symptom
- Tool returns `{"error": "permission denied"}` — agent says "file written successfully"
- Tool returns HTTP 404 wrapped in JSON — agent continues to the next step assuming success
- Tool returns `{"status": "failed", "reason": "quota exceeded"}` — agent ignores `status` field
- Agent writes a downstream artifact based on a tool result that was actually an error
- Tool raises an exception — framework catches it and returns `{"is_error": true}` — agent doesn't notice
- Agent produces a final answer citing data from a tool call that silently failed

## Root Cause
The model reads tool results as text and tries to make sense of them. When a result looks mostly like a success response but contains an error field, the model may focus on the non-error parts and miss the failure signal. This is especially common when: the error field is deeply nested, the result is large and the error is at the end, the field name is ambiguous (`status: "failed"` vs `ok: false`), or the model is primed to expect success. The fix is to validate tool results before injection and surface errors explicitly.

## Fix

### Option 1: Tool result validator — detect errors before model sees them

```python
import json
import re
from typing import Any

ERROR_FIELD_PATTERNS = [
    # Common error field names and values
    (["error"], lambda v: v is not None and v is not False and v != "" and v != 0),
    (["err"], lambda v: v is not None and v is not False),
    (["success"], lambda v: v is False),
    (["ok"], lambda v: v is False),
    (["status"], lambda v: isinstance(v, str) and v.lower() in (
        "error", "failed", "failure", "fail", "rejected", "denied"
    )),
    (["code"], lambda v: isinstance(v, int) and v >= 400),
    (["statusCode"], lambda v: isinstance(v, int) and v >= 400),
    (["status_code"], lambda v: isinstance(v, int) and v >= 400),
    (["is_error"], lambda v: v is True),
    (["hasError"], lambda v: v is True),
]

def detect_tool_error(result: Any) -> tuple[bool, str | None]:
    """
    Check a tool result for error signals.
    Returns (is_error, error_message | None).
    Handles both dict/JSON results and plain error strings.
    """
    if isinstance(result, str):
        # Plain error string patterns
        lower = result.lower().strip()
        error_phrases = [
            "error:", "permission denied", "access denied", "not found",
            "internal server error", "connection refused", "timeout",
            "quota exceeded", "rate limited", "unauthorized", "forbidden"
        ]
        for phrase in error_phrases:
            if phrase in lower and len(result) < 500:
                return True, result
        return False, None

    if isinstance(result, dict):
        for fields, is_error_fn in ERROR_FIELD_PATTERNS:
            value = result
            found = True
            for field in fields:
                if not isinstance(value, dict) or field not in value:
                    found = False
                    break
                value = value[field]
            if found and is_error_fn(value):
                # Extract error message
                msg = (
                    result.get("message") or
                    result.get("error_message") or
                    result.get("detail") or
                    result.get("details") or
                    str(value)
                )
                return True, msg

    return False, None

def wrap_tool_result_for_agent(
    tool_name: str,
    raw_result: Any,
    tool_use_id: str
) -> dict:
    """
    Wrap a tool result for injection into the agent conversation.
    Surfaces errors explicitly so the model cannot miss them.
    """
    is_error, error_msg = detect_tool_error(raw_result)

    if is_error:
        # Make the error impossible to miss
        error_content = (
            f"[TOOL ERROR] Tool '{tool_name}' failed:\n"
            f"{error_msg}\n\n"
            f"Do not proceed as if this tool call succeeded. "
            f"Handle this error before continuing."
        )
        print(f"Tool error detected in '{tool_name}': {error_msg}")
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "content": error_content
        }

    # Success — return normally
    if isinstance(raw_result, (dict, list)):
        content = json.dumps(raw_result)
    else:
        content = str(raw_result)

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content
    }
```

### Option 2: Typed tool results — enforce success/failure schema

```python
from pydantic import BaseModel, Field
from typing import Optional, Generic, TypeVar, Any
import json

T = TypeVar("T")

class ToolResult(BaseModel, Generic[T]):
    """
    Strongly-typed tool result — every tool must return this.
    Makes success/failure impossible to confuse.
    """
    success: bool
    data: Optional[T] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

    def to_agent_content(self, tool_name: str) -> str:
        """Format for injection into agent context"""
        if not self.success:
            return (
                f"TOOL FAILED: {tool_name}\n"
                f"Error: {self.error}\n"
                f"Code: {self.error_code or 'unknown'}\n"
                f"Action required: Do not proceed — address this error."
            )
        return json.dumps(self.data) if self.data is not None else "Success (no data returned)"

def make_tool_success(data: Any) -> ToolResult:
    return ToolResult(success=True, data=data)

def make_tool_error(error: str, code: str = None) -> ToolResult:
    return ToolResult(success=False, error=error, error_code=code)

# Tool implementations always return ToolResult:
async def write_file_tool(path: str, content: str) -> ToolResult:
    try:
        import aiofiles
        async with aiofiles.open(path, "w") as f:
            await f.write(content)
        return make_tool_success({"path": path, "bytes_written": len(content.encode())})
    except PermissionError as e:
        return make_tool_error(f"Permission denied writing to {path}: {e}", code="PERMISSION_DENIED")
    except OSError as e:
        return make_tool_error(f"OS error writing {path}: {e}", code="OS_ERROR")

async def call_api_tool(endpoint: str, payload: dict) -> ToolResult:
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(endpoint, json=payload, timeout=30.0)
        if response.status_code >= 400:
            return make_tool_error(
                f"API returned {response.status_code}: {response.text[:200]}",
                code=f"HTTP_{response.status_code}"
            )
        return make_tool_success(response.json())
    except httpx.TimeoutException:
        return make_tool_error(f"Request to {endpoint} timed out", code="TIMEOUT")
    except httpx.RequestError as e:
        return make_tool_error(f"Request failed: {e}", code="NETWORK_ERROR")

# Agent loop using typed results:
def process_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> dict:
    """Execute tool and format result — errors are always surfaced"""
    import asyncio
    tools = {"write_file": write_file_tool, "call_api": call_api_tool}

    if tool_name not in tools:
        result = make_tool_error(f"Unknown tool: {tool_name}", code="UNKNOWN_TOOL")
    else:
        result = asyncio.run(tools[tool_name](**tool_input))

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": not result.success,
        "content": result.to_agent_content(tool_name)
    }
```

### Option 3: Error assertion after tool call — verify success before proceeding

```python
import anthropic
import json
from typing import Callable

client = anthropic.Anthropic()

ERROR_ASSERTION_PROMPT = """After each tool call, before proceeding:
1. Read the tool result carefully
2. Check for any error, failure, or warning signals
3. If the tool failed, say "TOOL FAILED: [reason]" and stop — do not continue as if it succeeded
4. Only proceed to the next step after confirming the tool succeeded

Signs of failure to watch for:
- Any field named "error", "err", "failure", "exception" with a non-null value
- status/ok/success field set to false, "failed", "error", or similar
- HTTP status codes 4xx or 5xx in the response
- Exception messages or stack traces
- Empty results when content was expected
- "permission denied", "not found", "unauthorized" anywhere in the response

If you're unsure whether a tool succeeded, treat it as failed and report the uncertainty."""

def run_agent_with_error_assertion(
    messages: list[dict],
    tools: list[dict],
    system: str,
    execute_tool: Callable,
    model: str = "claude-sonnet-4-6",
    max_steps: int = 20
) -> str:
    """Agent loop with error assertion system prompt"""
    full_system = f"{system}\n\n{ERROR_ASSERTION_PROMPT}"

    for step in range(max_steps):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=full_system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            return response.content[0].text if response.content else ""

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw_result = execute_tool(block.name, block.input)

            # Validate before injection
            is_error, error_msg = detect_tool_error(raw_result)
            tool_result = wrap_tool_result_for_agent(block.name, raw_result, block.id)
            tool_results.append(tool_result)

            if is_error:
                print(f"Step {step}: Tool '{block.name}' failed — agent will see explicit error")

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Max steps reached"
```

### Option 4: Post-tool verification — have agent confirm success explicitly

```python
VERIFICATION_TOOLS = [
    {
        "name": "confirm_tool_result",
        "description": (
            "Call this IMMEDIATELY after any tool call that modifies state "
            "(write file, send message, update database, etc.). "
            "This confirms whether the previous tool actually succeeded. "
            "Do not proceed to the next step without calling this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool that was just called"
                },
                "expected_outcome": {
                    "type": "string",
                    "description": "What you expected to happen if the tool succeeded"
                },
                "tool_result_summary": {
                    "type": "string",
                    "description": "Brief summary of what the tool result showed"
                }
            },
            "required": ["tool_name", "expected_outcome", "tool_result_summary"]
        }
    }
]

def handle_confirm_tool_result(tool_input: dict, recent_tool_results: list) -> str:
    """
    Evaluate whether the most recent tool call succeeded.
    Returns a clear success/failure verdict.
    """
    if not recent_tool_results:
        return "ERROR: No recent tool results to confirm"

    last_result = recent_tool_results[-1]
    is_error = last_result.get("is_error", False)
    content = last_result.get("content", "")

    if is_error or "[TOOL ERROR]" in str(content):
        return (
            f"VERIFICATION FAILED: Tool '{tool_input['tool_name']}' did not succeed.\n"
            f"The tool returned an error. Do not proceed — handle the failure."
        )

    # Additional content-based check
    content_lower = str(content).lower()
    failure_signals = ["error", "failed", "failure", "denied", "unauthorized", "not found"]
    found_signals = [s for s in failure_signals if s in content_lower]

    if found_signals and len(content) < 1000:
        return (
            f"VERIFICATION WARNING: Tool result may indicate failure.\n"
            f"Detected signals: {found_signals}\n"
            f"Review the result carefully before proceeding."
        )

    return (
        f"VERIFIED: Tool '{tool_input['tool_name']}' appears to have succeeded.\n"
        f"Expected: {tool_input['expected_outcome']}\n"
        f"Safe to proceed to next step."
    )
```

### Option 5: Tool result normalization — convert all formats to standard structure

```python
from typing import Any

class ToolResultNormalizer:
    """
    Normalize tool results from any format into a standard structure.
    Makes error detection consistent regardless of the tool's output format.
    """

    def normalize(self, tool_name: str, raw: Any) -> dict:
        """
        Returns: {"success": bool, "data": Any, "error": str | None, "raw": Any}
        """
        if raw is None:
            return {"success": False, "data": None, "error": "Tool returned null", "raw": raw}

        if isinstance(raw, dict):
            return self._normalize_dict(raw)

        if isinstance(raw, str):
            return self._normalize_string(raw)

        if isinstance(raw, list):
            return {"success": True, "data": raw, "error": None, "raw": raw}

        return {"success": True, "data": raw, "error": None, "raw": raw}

    def _normalize_dict(self, result: dict) -> dict:
        # Explicit success fields
        if "success" in result:
            success = bool(result["success"])
            return {
                "success": success,
                "data": result.get("data") or result.get("result") or (result if success else None),
                "error": result.get("error") or result.get("message") if not success else None,
                "raw": result
            }

        if "ok" in result:
            ok = bool(result["ok"])
            return {
                "success": ok,
                "data": result if ok else None,
                "error": result.get("error") or result.get("message") if not ok else None,
                "raw": result
            }

        # HTTP-style status codes
        code = result.get("status_code") or result.get("statusCode") or result.get("code")
        if isinstance(code, int):
            success = code < 400
            return {
                "success": success,
                "data": result if success else None,
                "error": f"HTTP {code}: {result.get('message', result.get('error', ''))}" if not success else None,
                "raw": result
            }

        # Error field present
        if "error" in result and result["error"]:
            return {
                "success": False,
                "data": None,
                "error": str(result["error"]),
                "raw": result
            }

        # No error signals — assume success
        return {"success": True, "data": result, "error": None, "raw": result}

    def _normalize_string(self, result: str) -> dict:
        lower = result.lower().strip()
        failure_phrases = ["error:", "failed:", "exception:", "traceback", "permission denied", "not found"]
        if any(p in lower for p in failure_phrases):
            return {"success": False, "data": None, "error": result, "raw": result}
        return {"success": True, "data": result, "error": None, "raw": result}

    def format_for_context(self, normalized: dict, tool_name: str) -> str:
        """Format normalized result for injection into agent context"""
        if not normalized["success"]:
            return (
                f"❌ TOOL FAILED: {tool_name}\n"
                f"Error: {normalized['error']}\n"
                f"This tool call did not succeed. Do not use its output."
            )
        data = normalized["data"]
        if isinstance(data, (dict, list)):
            return f"✓ Tool succeeded:\n{json.dumps(data, indent=2)}"
        return f"✓ Tool succeeded: {data}"

normalizer = ToolResultNormalizer()
```

### Option 6: Failure cascade prevention — stop agent when critical tool fails

```python
class CriticalToolGuard:
    """
    Prevents the agent from continuing after a critical tool fails.
    Some tools are "critical path" — their failure should halt the agent.
    """

    CRITICAL_TOOLS = {
        "write_to_database", "send_payment", "deploy_code",
        "delete_record", "send_email", "publish_document"
    }

    def __init__(self, critical_tools: set[str] = None):
        self.critical_tools = critical_tools or self.CRITICAL_TOOLS
        self._failed_critical: list[str] = []

    def check_and_gate(
        self,
        tool_name: str,
        tool_result: dict,
        tool_use_id: str
    ) -> dict:
        """
        Check tool result. If critical tool failed, inject halt instruction.
        Returns the tool result dict to inject into the conversation.
        """
        is_error = tool_result.get("is_error", False)
        content = tool_result.get("content", "")
        error_detected, error_msg = detect_tool_error(content)
        actually_failed = is_error or error_detected

        if actually_failed and tool_name in self.critical_tools:
            self._failed_critical.append(tool_name)
            halt_message = (
                f"[CRITICAL FAILURE] Tool '{tool_name}' failed and is required for this task.\n"
                f"Error: {error_msg or content[:300]}\n\n"
                f"STOP IMMEDIATELY. Do not proceed to subsequent steps.\n"
                f"Report this failure to the user and explain what happened.\n"
                f"Do not attempt workarounds or alternative approaches without explicit instruction."
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": True,
                "content": halt_message
            }

        return tool_result

    @property
    def has_critical_failure(self) -> bool:
        return len(self._failed_critical) > 0

guard = CriticalToolGuard()
```

## Error Detection Coverage by Result Format

| Result Format | Example | Detection Method |
|---|---|---|
| Explicit error field | `{"error": "permission denied"}` | Field name check |
| ok/success boolean | `{"ok": false, "error": "..."}` | Boolean field check |
| HTTP status in response | `{"status_code": 403}` | Numeric code check |
| Error string | `"Error: connection refused"` | Pattern matching |
| Nested error | `{"data": null, "meta": {"error": true}}` | Recursive field scan |
| Anthropic is_error | `{"type": "tool_result", "is_error": true}` | is_error flag check |

## Expected Token Savings
Agent proceeds on silent tool failure → produces wrong output → user corrects → debug: ~18,000 tokens
Error surfaced explicitly → agent handles error immediately: 0 downstream corruption

## Environment
- Any agent using tool use; error detection is especially critical for tools that write data, send messages, or call external APIs where the failure mode is a valid-looking JSON response with an error field — the model must be forced to notice tool failures before continuing
- Source: direct experience; silent tool failure propagation is the root cause of the most confusing agent bugs — the model is reasoning correctly from wrong premises
