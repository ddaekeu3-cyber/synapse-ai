---
title: "Agent Doesn't Implement Tool Precondition Validation"
description: "How to validate that all required preconditions are met before executing a tool call, preventing partial execution, data corruption, and hard-to-debug failures."
categories: [tool-failure]
difficulty: intermediate
---

Calling a tool without checking its preconditions leads to runtime errors deep in execution—after side effects have already occurred. Validating preconditions before each tool call lets you fail fast, return a clear error message to the model, and avoid corrupt state.

## Solution 1: Declarative Precondition Registry

Define preconditions as async predicates alongside tool definitions and check them before execution.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class Precondition:
    description: str
    check: Callable[[dict], Awaitable[bool]]
    error_message: str


@dataclass
class ToolSpec:
    name: str
    preconditions: list[Precondition] = field(default_factory=list)


# Simulated environment state
_db_connected = True
_user_authenticated = True
_rate_limit_remaining = 10


async def check_db_connected(_args: dict) -> bool:
    return _db_connected


async def check_authenticated(_args: dict) -> bool:
    return _user_authenticated


async def check_rate_limit(_args: dict) -> bool:
    global _rate_limit_remaining
    return _rate_limit_remaining > 0


TOOL_SPECS: dict[str, ToolSpec] = {
    "query_database": ToolSpec(
        name="query_database",
        preconditions=[
            Precondition(
                description="Database must be connected",
                check=check_db_connected,
                error_message="Database connection is not available. Retry after reconnection.",
            ),
            Precondition(
                description="User must be authenticated",
                check=check_authenticated,
                error_message="User session is not authenticated. Request a new session token.",
            ),
        ],
    ),
    "send_notification": ToolSpec(
        name="send_notification",
        preconditions=[
            Precondition(
                description="Rate limit must not be exhausted",
                check=check_rate_limit,
                error_message="Notification rate limit exceeded. Wait before sending more.",
            ),
        ],
    ),
}


async def validate_preconditions(tool_name: str, args: dict) -> str | None:
    """Returns None if all preconditions pass, or an error string."""
    spec = TOOL_SPECS.get(tool_name)
    if not spec:
        return None  # No preconditions registered

    for pre in spec.preconditions:
        ok = await pre.check(args)
        if not ok:
            return f"Precondition failed for '{tool_name}': {pre.error_message}"

    return None


async def execute_tool(tool_name: str, args: dict) -> str:
    error = await validate_preconditions(tool_name, args)
    if error:
        return f"[PRECONDITION_FAILURE] {error}"

    # Actual tool execution
    return f"[{tool_name}] executed with args: {args}"


async def agent_loop(query: str) -> str:
    tools = [
        {
            "name": "query_database",
            "description": "Query the database",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        }
    ]
    messages = [{"role": "user", "content": query}]

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = await execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})
```

## Solution 2: Argument-Level Precondition Validation

Validate argument values (ranges, formats, mutual exclusions) before handing off to the tool.

```python
import asyncio
import re
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()


class ArgValidationError(Exception):
    pass


def validate_sql_query(sql: str) -> None:
    """Reject destructive SQL in read-only tools."""
    forbidden = ["DROP", "DELETE", "TRUNCATE", "ALTER", "INSERT", "UPDATE"]
    upper = sql.upper()
    for keyword in forbidden:
        if keyword in upper:
            raise ArgValidationError(
                f"Tool 'query_database' is read-only. Found forbidden keyword: {keyword}"
            )
    if len(sql) > 4000:
        raise ArgValidationError("SQL query exceeds 4000 character limit.")


def validate_email(email: str) -> None:
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(pattern, email):
        raise ArgValidationError(f"Invalid email format: {email!r}")


def validate_date_range(start: str, end: str) -> None:
    from datetime import date
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except ValueError as exc:
        raise ArgValidationError(f"Invalid date format: {exc}") from exc
    if s > e:
        raise ArgValidationError(f"start_date ({start}) must be <= end_date ({end})")
    if (e - s).days > 365:
        raise ArgValidationError("Date range cannot exceed 365 days.")


ARG_VALIDATORS = {
    "query_database": lambda args: validate_sql_query(args.get("sql", "")),
    "send_email": lambda args: validate_email(args.get("to", "")),
    "fetch_report": lambda args: validate_date_range(
        args.get("start_date", ""), args.get("end_date", "")
    ),
}


async def validated_tool_call(tool_name: str, args: dict) -> str:
    validator = ARG_VALIDATORS.get(tool_name)
    if validator:
        try:
            validator(args)
        except ArgValidationError as e:
            return f"[VALIDATION_ERROR] {e}"

    return f"[{tool_name}] success with args: {args}"


async def main():
    # Simulate validation
    tests = [
        ("query_database", {"sql": "SELECT * FROM users"}),
        ("query_database", {"sql": "DROP TABLE users"}),
        ("send_email", {"to": "invalid-email"}),
        ("send_email", {"to": "user@example.com"}),
        ("fetch_report", {"start_date": "2024-01-01", "end_date": "2024-06-01"}),
        ("fetch_report", {"start_date": "2024-12-01", "end_date": "2024-01-01"}),
    ]
    for name, args in tests:
        result = await validated_tool_call(name, args)
        print(f"{name}({args}): {result}")


asyncio.run(main())
```

## Solution 3: State Machine Preconditions

Tools may only be called when the agent is in the correct state. Enforce state transitions before execution.

```python
import asyncio
from enum import Enum
from typing import set
import anthropic

client = anthropic.AsyncAnthropic()


class AgentState(Enum):
    IDLE = "idle"
    AUTHENTICATED = "authenticated"
    SESSION_OPEN = "session_open"
    PROCESSING = "processing"


# Which states each tool requires
TOOL_REQUIRED_STATES: dict[str, set] = {
    "login": {AgentState.IDLE},
    "open_session": {AgentState.AUTHENTICATED},
    "query": {AgentState.SESSION_OPEN, AgentState.PROCESSING},
    "close_session": {AgentState.SESSION_OPEN, AgentState.PROCESSING},
    "logout": {AgentState.AUTHENTICATED, AgentState.IDLE},
}

# State transitions triggered by successful tool calls
TOOL_TRANSITIONS: dict[str, AgentState] = {
    "login": AgentState.AUTHENTICATED,
    "open_session": AgentState.SESSION_OPEN,
    "query": AgentState.PROCESSING,
    "close_session": AgentState.AUTHENTICATED,
    "logout": AgentState.IDLE,
}


class StatefulToolRunner:
    def __init__(self):
        self.state = AgentState.IDLE

    async def run(self, tool_name: str, args: dict) -> str:
        required = TOOL_REQUIRED_STATES.get(tool_name)
        if required and self.state not in required:
            return (
                f"[STATE_ERROR] Cannot call '{tool_name}' in state '{self.state.value}'. "
                f"Required state(s): {[s.value for s in required]}"
            )

        # Execute tool
        result = f"[{tool_name}] executed (state: {self.state.value})"

        # Apply state transition
        if tool_name in TOOL_TRANSITIONS:
            self.state = TOOL_TRANSITIONS[tool_name]

        return result


async def main():
    runner = StatefulToolRunner()

    # Correct sequence
    for tool in ["login", "open_session", "query", "close_session"]:
        r = await runner.run(tool, {})
        print(r)

    print()

    # Wrong sequence (query without login)
    runner2 = StatefulToolRunner()
    r = await runner2.run("query", {"sql": "SELECT 1"})
    print(r)


asyncio.run(main())
```

## Solution 4: Resource Availability Preconditions

Check that external resources (APIs, files, services) are reachable before making calls that depend on them.

```python
import asyncio
import time
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ResourceHealth:
    name: str
    last_checked: float = 0.0
    healthy: bool = True
    ttl: float = 30.0  # Re-check interval in seconds

    def is_stale(self) -> bool:
        return time.monotonic() - self.last_checked > self.ttl


# Registry of resource health
_resource_health: dict[str, ResourceHealth] = {
    "payment_api": ResourceHealth("payment_api"),
    "email_service": ResourceHealth("email_service"),
    "cdn": ResourceHealth("cdn"),
}


async def check_resource(name: str) -> bool:
    """Simulate a health check (ping, HEAD request, etc.)."""
    await asyncio.sleep(0.01)  # Simulate latency
    # In production: aiohttp HEAD request, ping, etc.
    return True  # Simulate healthy


async def ensure_resource_healthy(name: str) -> str | None:
    health = _resource_health.get(name)
    if health is None:
        return None  # Unknown resource — allow (no precondition)

    if health.is_stale():
        health.healthy = await check_resource(name)
        health.last_checked = time.monotonic()

    if not health.healthy:
        return f"Resource '{name}' is currently unavailable. Try again later."

    return None


# Map tools to their required resources
TOOL_RESOURCES: dict[str, list[str]] = {
    "charge_card": ["payment_api"],
    "send_receipt": ["email_service", "payment_api"],
    "serve_asset": ["cdn"],
}


async def resource_checked_tool(tool_name: str, args: dict) -> str:
    resources = TOOL_RESOURCES.get(tool_name, [])
    errors = await asyncio.gather(*[ensure_resource_healthy(r) for r in resources])
    failures = [e for e in errors if e]

    if failures:
        return "[RESOURCE_UNAVAILABLE] " + "; ".join(failures)

    return f"[{tool_name}] executed successfully"


async def main():
    tools = ["charge_card", "send_receipt", "serve_asset"]
    results = await asyncio.gather(*[resource_checked_tool(t, {}) for t in tools])
    for tool, result in zip(tools, results):
        print(f"{tool}: {result}")


asyncio.run(main())
```

## Solution 5: Schema + Semantic Precondition Pipeline

Chain schema validation (types/required fields), then semantic validation (business logic), then resource checks.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ValidationStage:
    name: str
    check: Callable[[str, dict], Awaitable[str | None]]


async def schema_check(tool_name: str, args: dict) -> str | None:
    """Validate required fields and types."""
    schemas = {
        "transfer_funds": {
            "required": ["from_account", "to_account", "amount"],
            "types": {"amount": (int, float)},
        }
    }
    schema = schemas.get(tool_name)
    if not schema:
        return None
    for field in schema.get("required", []):
        if field not in args:
            return f"Missing required argument: '{field}'"
    for field, expected in schema.get("types", {}).items():
        if field in args and not isinstance(args[field], expected):
            return f"'{field}' must be {expected}, got {type(args[field]).__name__}"
    return None


async def semantic_check(tool_name: str, args: dict) -> str | None:
    """Validate business rules."""
    if tool_name == "transfer_funds":
        amount = args.get("amount", 0)
        if amount <= 0:
            return "Transfer amount must be positive."
        if amount > 1_000_000:
            return "Transfer amount exceeds single-transaction limit of $1,000,000."
        if args.get("from_account") == args.get("to_account"):
            return "Source and destination accounts must be different."
    return None


async def resource_check(tool_name: str, args: dict) -> str | None:
    """Verify downstream services are available."""
    await asyncio.sleep(0.005)  # Simulate health ping
    return None  # All healthy


PIPELINE: list[ValidationStage] = [
    ValidationStage("schema", schema_check),
    ValidationStage("semantic", semantic_check),
    ValidationStage("resource", resource_check),
]


async def run_pipeline(tool_name: str, args: dict) -> str | None:
    for stage in PIPELINE:
        error = await stage.check(tool_name, args)
        if error:
            return f"[{stage.name.upper()}_ERROR] {error}"
    return None


async def execute(tool_name: str, args: dict) -> str:
    error = await run_pipeline(tool_name, args)
    if error:
        return error
    return f"[{tool_name}] SUCCESS: {json.dumps(args)}"


async def main():
    cases = [
        ("transfer_funds", {"from_account": "A1", "to_account": "A2", "amount": 500.0}),
        ("transfer_funds", {"from_account": "A1", "to_account": "A2"}),  # Missing amount
        ("transfer_funds", {"from_account": "A1", "to_account": "A1", "amount": 100}),  # Same account
        ("transfer_funds", {"from_account": "A1", "to_account": "A2", "amount": -50}),  # Negative
    ]
    for name, args in cases:
        result = await execute(name, args)
        print(f"{args} → {result}")


asyncio.run(main())
```

## Solution 6: LLM-Assisted Precondition Reasoning

For complex tools whose preconditions are difficult to express programmatically, use a lightweight model to reason about whether it's safe to proceed.

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

TOOL_RISK_POLICIES = {
    "delete_records": {
        "risk": "HIGH",
        "policy": (
            "Only proceed if: (1) a backup was confirmed in this session, "
            "(2) the target is explicitly identified as test/staging data, "
            "(3) the user has explicitly confirmed deletion."
        ),
    },
    "send_bulk_email": {
        "risk": "MEDIUM",
        "policy": (
            "Only proceed if: (1) recipient count < 1000, "
            "(2) content has been previewed, "
            "(3) unsubscribe link is confirmed present."
        ),
    },
}


async def llm_precondition_check(
    tool_name: str, args: dict, conversation_context: str
) -> tuple[bool, str]:
    policy = TOOL_RISK_POLICIES.get(tool_name)
    if not policy:
        return True, "No policy — proceeding."

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Tool: {tool_name}\n"
                    f"Risk level: {policy['risk']}\n"
                    f"Policy: {policy['policy']}\n\n"
                    f"Tool arguments: {json.dumps(args)}\n\n"
                    f"Conversation context:\n{conversation_context}\n\n"
                    f"Based on the policy, should this tool call proceed? "
                    f"Reply with exactly 'ALLOW' or 'BLOCK: <reason>'."
                ),
            }
        ],
    )

    text = resp.content[0].text.strip()
    if text.startswith("ALLOW"):
        return True, "Policy check passed."
    return False, text.replace("BLOCK: ", "", 1)


async def guarded_tool_call(
    tool_name: str, args: dict, context: str
) -> str:
    allowed, reason = await llm_precondition_check(tool_name, args, context)
    if not allowed:
        return f"[LLM_PRECONDITION_BLOCKED] {reason}"
    return f"[{tool_name}] executed: {args}"


async def main():
    # Simulated conversation where user confirmed backup
    context_with_backup = "User said: I've confirmed the backup is complete. Please delete the test records."
    context_without_backup = "User said: Just clean up the old records."

    r1 = await guarded_tool_call(
        "delete_records", {"table": "test_users", "filter": "created_at < '2024-01-01'"}, context_with_backup
    )
    r2 = await guarded_tool_call(
        "delete_records", {"table": "production_orders"}, context_without_backup
    )
    print(f"With backup context: {r1}")
    print(f"Without backup context: {r2}")


asyncio.run(main())
```

## Comparison

| Solution | Validation type | Latency | Flexibility | Best for |
|---|---|---|---|---|
| **Declarative registry** | Async predicates | Low | High | General-purpose preconditions |
| **Argument-level** | Value rules | Near-zero | Medium | Type/format/range checks |
| **State machine** | State transitions | Near-zero | Medium | Sequential workflow tools |
| **Resource availability** | Health checks | Low | High | External dependency tools |
| **Schema + semantic pipeline** | Multi-stage | Low | Very high | High-stakes business operations |
| **LLM-assisted reasoning** | Policy + context | Medium (Haiku) | Very high | Complex, context-dependent rules |

Start with **argument-level validation** (Solution 2) for immediate protection against malformed inputs. Add **declarative registry** (Solution 1) for reusable preconditions. Use **LLM-assisted reasoning** (Solution 6) only for high-risk tools where business rules are too complex for code.
