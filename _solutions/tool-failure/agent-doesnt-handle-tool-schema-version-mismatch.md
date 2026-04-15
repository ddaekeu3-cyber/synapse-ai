---
layout: solution
title: "Agent Doesn't Handle Tool Schema Version Mismatch"
category: tool-failure
description: "When an external tool's API schema changes, the agent silently passes wrong arguments or fails with cryptic errors because it caches stale tool definitions."
tags: [tool-failure, schema, versioning, validation, drift, pydantic]
---

# Agent Doesn't Handle Tool Schema Version Mismatch

Agents define tools at startup and never re-validate them against the actual service they call. When the downstream API evolves — new required fields, renamed parameters, removed endpoints — the agent continues calling with a stale schema. The result is silent wrong-field usage, 422 validation errors from the API, or successful calls that silently drop data.

## Why This Happens

Tool schemas are typically hardcoded as dicts in source code and only updated during deployments. There's no runtime schema negotiation, no version header checking, and no drift detection. The agent has no feedback loop between what the tool definition says and what the service actually accepts.

---

## Option 1: Schema Version Header Validation

Check a `X-Schema-Version` header (or equivalent) from the tool's HTTP response and fail fast if it doesn't match the expected version.

```python
import httpx
import anthropic
from anthropic.types import ToolParam

client = anthropic.Anthropic()

EXPECTED_SCHEMA_VERSION = "2"
WEATHER_API_URL = "https://api.example.com/weather"

# Tool schema pinned to v2
WEATHER_TOOL: ToolParam = {
    "name": "get_weather",
    "description": "Get current weather for a location",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City name"},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location", "units"],  # v2: units is now required
    },
}


def call_weather_tool(location: str, units: str) -> dict:
    resp = httpx.get(
        WEATHER_API_URL,
        params={"location": location, "units": units},
        timeout=10,
    )
    resp.raise_for_status()

    # Check schema version
    server_version = resp.headers.get("X-Schema-Version", "unknown")
    if server_version != EXPECTED_SCHEMA_VERSION:
        raise ValueError(
            f"Tool schema version mismatch: "
            f"agent expects v{EXPECTED_SCHEMA_VERSION}, "
            f"server returned v{server_version}. "
            f"Update tool definition before proceeding."
        )

    return resp.json()


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[WEATHER_TOOL],
        messages=[{"role": "user", "content": user_message}],
    )

    if response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        try:
            result = call_weather_tool(**tool_use.input)
            tool_result_content = str(result)
        except ValueError as e:
            # Schema mismatch — report to model so it can inform the user
            tool_result_content = f"ERROR: {e}"

        final_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=[WEATHER_TOOL],
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": tool_result_content,
                        }
                    ],
                },
            ],
        )
        return final_response.content[0].text

    return response.content[0].text
```

**Expected Token Savings:** Fails fast on schema mismatch instead of burning tokens on retries with wrong arguments.

**Environment:** REST API tools with version headers; any HTTP-based tool integration.

---

## Option 2: Runtime Schema Fetching and Diff Detection

Fetch the tool's JSON Schema from a discovery endpoint at startup and diff it against the hardcoded definition.

```python
import json
import httpx
import anthropic
from anthropic.types import ToolParam

client = anthropic.Anthropic()

SCHEMA_DISCOVERY_URL = "https://api.example.com/tools/schema"


def fetch_live_schema(tool_name: str) -> dict:
    """Fetch the current schema from the tool's discovery endpoint."""
    resp = httpx.get(f"{SCHEMA_DISCOVERY_URL}/{tool_name}", timeout=5)
    resp.raise_for_status()
    return resp.json()


def diff_schemas(local: dict, remote: dict, path: str = "") -> list[str]:
    """Return a list of differences between local and remote schemas."""
    diffs = []

    if type(local) != type(remote):
        diffs.append(f"{path}: type changed {type(local).__name__} -> {type(remote).__name__}")
        return diffs

    if isinstance(local, dict):
        all_keys = set(local) | set(remote)
        for key in all_keys:
            if key not in local:
                diffs.append(f"{path}.{key}: NEW field in remote")
            elif key not in remote:
                diffs.append(f"{path}.{key}: REMOVED from remote")
            else:
                diffs.extend(diff_schemas(local[key], remote[key], f"{path}.{key}"))
    elif isinstance(local, list):
        for i, (l, r) in enumerate(zip(local, remote)):
            diffs.extend(diff_schemas(l, r, f"{path}[{i}]"))
    else:
        if local != remote:
            diffs.append(f"{path}: '{local}' -> '{remote}'")

    return diffs


HARDCODED_TOOL: ToolParam = {
    "name": "search_database",
    "description": "Search the product database",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    },
}


def validate_tool_schema(tool: ToolParam) -> ToolParam:
    """Fetch live schema and warn/error on drift."""
    try:
        live_schema = fetch_live_schema(tool["name"])
        diffs = diff_schemas(
            tool["input_schema"],
            live_schema.get("input_schema", live_schema),
        )
        if diffs:
            print(f"WARNING: Schema drift detected for '{tool['name']}':")
            for diff in diffs:
                print(f"  {diff}")
            # Optionally use the live schema instead
            if live_schema.get("input_schema"):
                print("  -> Using live schema from server")
                return {**tool, "input_schema": live_schema["input_schema"]}
    except Exception as e:
        print(f"WARNING: Could not validate schema for '{tool['name']}': {e}")

    return tool


# At agent startup, validate all tools
def initialize_tools(tools: list[ToolParam]) -> list[ToolParam]:
    return [validate_tool_schema(t) for t in tools]


validated_tools = initialize_tools([HARDCODED_TOOL])
```

**Expected Token Savings:** Catches schema drift before any API calls are made; prevents failed tool calls and retry loops.

**Environment:** Microservice architectures where tool APIs are versioned independently.

---

## Option 3: Pydantic Schema Guards with Graceful Degradation

Use Pydantic models to validate both the arguments before calling and the response after, with fallback behavior when validation fails.

```python
from __future__ import annotations
import anthropic
from pydantic import BaseModel, ValidationError, field_validator
from typing import Any

client = anthropic.Anthropic()


class SearchInput(BaseModel):
    """v2 schema: category is now required."""
    query: str
    category: str  # new required field in v2
    limit: int = 10

    @field_validator("limit")
    @classmethod
    def limit_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("limit must be 1-100")
        return v


class SearchResult(BaseModel):
    items: list[dict]
    total: int
    page: int = 1


def search_database(raw_input: dict) -> dict:
    """Validates tool input against current schema before executing."""
    try:
        validated = SearchInput(**raw_input)
    except ValidationError as exc:
        missing = [e["loc"][0] for e in exc.errors() if e["type"] == "missing"]
        extra = [e["loc"][0] for e in exc.errors() if e["type"] == "extra_forbidden"]
        raise ValueError(
            f"Tool input schema mismatch: "
            f"missing fields={missing}, unexpected fields={extra}. "
            f"This usually means the agent's tool definition is outdated."
        ) from exc

    # Simulate API call
    return {
        "items": [{"id": 1, "name": f"Result for {validated.query}"}],
        "total": 1,
        "page": 1,
    }


def process_tool_call(tool_name: str, tool_input: dict) -> str:
    """Process tool call with schema version mismatch handling."""
    if tool_name == "search_database":
        try:
            result = search_database(tool_input)
            # Validate response schema too
            parsed = SearchResult(**result)
            return f"Found {parsed.total} results: {parsed.items}"
        except ValueError as e:
            return (
                f"Tool execution failed due to schema mismatch: {e}\n"
                f"Please retry with required fields: query, category, limit"
            )
        except ValidationError as e:
            return f"Unexpected response format from tool: {e}"
    return "Unknown tool"


# Tool definition (may be slightly out of date)
SEARCH_TOOL = {
    "name": "search_database",
    "description": "Search products. Required: query, category.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "category": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query", "category"],
    },
}


def run_search_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[SEARCH_TOOL],
        messages=messages,
    )

    if response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        result = process_tool_call(tool_use.name, tool_use.input)

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result}],
        })

        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )
        return final.content[0].text

    return response.content[0].text
```

**Expected Token Savings:** Validation errors caught before API call; model can self-correct with accurate error messages instead of retrying blindly.

**Environment:** Any tool integration; especially valuable when tool APIs evolve without agent redeployment.

---

## Option 4: Schema Hash Pinning with CI Drift Detection

Pin a hash of each tool schema at deploy time and check it on every agent startup to detect drift.

```python
import hashlib
import json
import os
import anthropic
from anthropic.types import ToolParam

client = anthropic.Anthropic()

# File storing accepted schema hashes: {"tool_name": "sha256_hash"}
SCHEMA_LOCK_FILE = ".tool-schema-lock.json"


def schema_hash(schema: dict) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def load_schema_lock() -> dict[str, str]:
    if os.path.exists(SCHEMA_LOCK_FILE):
        with open(SCHEMA_LOCK_FILE) as f:
            return json.load(f)
    return {}


def save_schema_lock(lock: dict[str, str]):
    with open(SCHEMA_LOCK_FILE, "w") as f:
        json.dump(lock, f, indent=2)
    print(f"Schema lock updated: {SCHEMA_LOCK_FILE}")


def verify_schema_lock(tools: list[ToolParam], update: bool = False) -> bool:
    """
    Verify tools against pinned hashes.
    Returns True if all schemas match. If update=True, updates the lock file.
    """
    lock = load_schema_lock()
    all_match = True

    for tool in tools:
        name = tool["name"]
        current_hash = schema_hash(tool["input_schema"])
        pinned_hash = lock.get(name)

        if pinned_hash is None:
            print(f"NEW tool '{name}' (hash: {current_hash}) — add to lock file")
            if update:
                lock[name] = current_hash
        elif pinned_hash != current_hash:
            print(
                f"SCHEMA DRIFT: '{name}' hash changed "
                f"{pinned_hash} -> {current_hash}"
            )
            all_match = False
            if update:
                lock[name] = current_hash
                print(f"  -> Updated lock for '{name}'")
        else:
            print(f"OK: '{name}' schema unchanged (hash: {current_hash})")

    if update:
        save_schema_lock(lock)

    return all_match


# Define tools
TOOLS: list[ToolParam] = [
    {
        "name": "get_user",
        "description": "Get user by ID",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "update_user",
        "description": "Update user fields",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "email": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
]


def initialize_agent():
    schemas_ok = verify_schema_lock(TOOLS)
    if not schemas_ok:
        raise RuntimeError(
            "Tool schema drift detected. "
            "Run with update=True to accept new schemas after review."
        )
    print("All tool schemas verified. Starting agent.")


if __name__ == "__main__":
    # First run: generate lock file
    verify_schema_lock(TOOLS, update=True)
    # Subsequent runs: verify
    initialize_agent()
```

**Expected Token Savings:** Catches drift in CI before deployment; zero runtime cost once schemas are stable.

**Environment:** CI/CD pipelines; production agents where schema stability is critical.

---

## Option 5: Tool Compatibility Probe on Startup

Make a dry-run call to each tool with minimal synthetic inputs to verify it accepts the current schema before serving real requests.

```python
import asyncio
import anthropic
from anthropic.types import ToolParam
from typing import Any, Callable, Awaitable

client = anthropic.AsyncAnthropic()


async def probe_tool(
    tool_name: str,
    probe_input: dict,
    executor: Callable[[dict], Awaitable[Any]],
) -> tuple[bool, str]:
    """
    Execute a probe call to verify the tool accepts our schema.
    Returns (ok, message).
    """
    try:
        result = await asyncio.wait_for(executor(probe_input), timeout=5.0)
        return True, f"OK: {tool_name} accepted probe input"
    except TypeError as e:
        return False, f"SCHEMA MISMATCH ({tool_name}): unexpected keyword argument — {e}"
    except KeyError as e:
        return False, f"SCHEMA MISMATCH ({tool_name}): missing expected field — {e}"
    except asyncio.TimeoutError:
        return False, f"TIMEOUT ({tool_name}): probe timed out after 5s"
    except Exception as e:
        # Non-schema errors (auth, network) are OK — schema is compatible
        if "validation" in str(e).lower() or "required" in str(e).lower():
            return False, f"VALIDATION ERROR ({tool_name}): {e}"
        return True, f"OK: {tool_name} returned non-schema error (likely auth/data)"


# Simulated tool executors
async def weather_executor(inputs: dict) -> dict:
    # Simulate API that now requires `units`
    if "units" not in inputs:
        raise KeyError("units")
    return {"temp": 72, "units": inputs["units"]}


async def search_executor(inputs: dict) -> dict:
    return {"results": [], "total": 0}


TOOL_REGISTRY = {
    "get_weather": {
        "tool_def": ToolParam(
            name="get_weather",
            description="Get weather",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    # Note: missing 'units' — will be caught by probe
                },
                "required": ["location"],
            },
        ),
        "executor": weather_executor,
        "probe_input": {"location": "test_city"},  # probe without units
    },
    "search": {
        "tool_def": ToolParam(
            name="search",
            description="Search",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        "executor": search_executor,
        "probe_input": {"query": "__probe__"},
    },
}


async def startup_probe_all() -> list[ToolParam]:
    """Probe all tools; exclude incompatible ones from the agent's tool list."""
    available_tools = []
    for name, config in TOOL_REGISTRY.items():
        ok, message = await probe_tool(name, config["probe_input"], config["executor"])
        print(message)
        if ok:
            available_tools.append(config["tool_def"])
        else:
            print(f"  -> Excluding '{name}' from agent tool list")
    return available_tools


async def main():
    available_tools = await startup_probe_all()
    print(f"\nAgent starting with {len(available_tools)} compatible tools")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=available_tools,
        messages=[{"role": "user", "content": "Search for laptops"}],
    )
    print(response.content[0].text if response.content else "No response")


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Incompatible tools removed before any user requests; eliminates failed tool call retries entirely.

**Environment:** Agent startup validation; microservices where tool backends deploy independently.

---

## Option 6: Schema Version Negotiation via System Prompt

Embed schema version constraints in the system prompt and instruct the model to check version compatibility before using tools.

```python
import anthropic
from anthropic.types import ToolParam

client = anthropic.Anthropic()

TOOL_VERSIONS = {
    "send_email": "3.1",
    "create_calendar_event": "2.0",
}

SYSTEM_PROMPT = """You are an assistant with access to tools.

## Tool Version Requirements
{version_block}

## Schema Mismatch Handling
Before using any tool, verify the tool version matches requirements.
If a tool call fails with a schema error (missing field, wrong type, unexpected field):
1. Report the exact error to the user
2. Do NOT retry with the same arguments
3. Explain which field is mismatched and what the current schema expects
4. Ask the user if they want to proceed with a different approach

Never silently swallow schema errors or assume a retry with identical args will succeed.
"""


def build_system_prompt() -> str:
    version_block = "\n".join(
        f"- {tool}: requires schema v{version}"
        for tool, version in TOOL_VERSIONS.items()
    )
    return SYSTEM_PROMPT.format(version_block=version_block)


TOOLS: list[ToolParam] = [
    {
        "name": "send_email",
        "description": "Send an email. Schema v3.1: requires 'to', 'subject', 'body', 'priority'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["to", "subject", "body", "priority"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create event. Schema v2.0: requires 'title', 'start_iso', 'end_iso', 'attendees'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "start_iso", "end_iso", "attendees"],
        },
    },
]


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=build_system_prompt(),
        tools=TOOLS,
        messages=[{"role": "user", "content": user_message}],
    )
    # Handle tool use loop
    messages = [{"role": "user", "content": user_message}]
    while response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        # In production, execute tool here; simulate response
        tool_result = f"Executed {tool_use.name} with {tool_use.input}"

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": tool_result}],
        })
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=build_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )

    return response.content[0].text


if __name__ == "__main__":
    print(run_agent("Send a high-priority email to alice@example.com about the meeting tomorrow."))
```

**Expected Token Savings:** Model self-reports schema issues rather than looping; reduces total turns when schema drift occurs.

**Environment:** Any Claude agent; adds schema awareness as a behavioral constraint without code changes.

---

## Comparison

| Option | Detection Point | Automatic Recovery | CI-Friendly | Multi-Tool |
|--------|----------------|-------------------|-------------|------------|
| 1. Version header | Per-call runtime | No | No | Per-endpoint |
| 2. Discovery diff | Agent startup | Uses live schema | Yes | Yes |
| 3. Pydantic guards | Per-call runtime | Graceful degradation | No | Per-tool |
| 4. Hash pinning | CI + startup | Lock file update | Yes | Yes |
| 5. Startup probe | Agent startup | Excludes broken tools | No | Yes |
| 6. System prompt | LLM behavior | Model-guided | No | Yes |
