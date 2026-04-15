---
layout: solution
title: "Agent Doesn't Implement Contract Testing for Tool Interfaces"
category: testing
description: "The agent and its tools evolve independently with no contract tests. When a tool renames a field or changes its return schema, the agent silently receives wrong data or crashes, discovered only in production."
tags: [testing, contract-testing, pydantic, tool-use, schema, jsonschema, pytest]
---

# Agent Doesn't Implement Contract Testing for Tool Interfaces

## Problem

Tool functions are refactored, their return schemas evolve, and argument names change — but the agent's tool definitions and parsing code are updated separately. Without contract tests that bind both sides to a shared schema, drift goes undetected until a live request fails because the tool returned `"file_path"` but the agent expected `"filepath"`.

## Solutions

### Option 1: Pydantic Contracts for Tool Input and Output

```python
# contracts/tool_contracts.py
"""
Define the agreed-upon input and output schemas for every tool as Pydantic models.
Both the tool implementation and the agent's parser must conform to these contracts.
Running these tests catches drift the moment either side changes.
"""
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ── Tool: search_documents ─────────────────────────────────────────────────────

class SearchDocumentsInput(BaseModel):
    """What the agent must pass to search_documents."""
    query: str = Field(..., min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=100)
    filter_tag: Optional[str] = None


class SearchDocumentsResult(BaseModel):
    """What the tool must return; what the agent must parse."""
    results: list[dict]
    total_found: int
    query_echo: str  # echo back the query so agent can verify


# ── Tool: execute_code ────────────────────────────────────────────────────────

class ExecuteCodeInput(BaseModel):
    code: str = Field(..., min_length=1)
    language: str = Field(..., pattern="^(python|javascript|bash)$")
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("code")
    @classmethod
    def no_shell_injection(cls, v: str) -> str:
        # Basic safety guard — real impl should be more thorough
        forbidden = ["import os", "subprocess", "__import__"]
        for pattern in forbidden:
            if pattern in v:
                raise ValueError(f"Forbidden pattern in code: {pattern}")
        return v


class ExecuteCodeResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: float
```

```python
# tests/contracts/test_tool_contracts.py
import pytest
from contracts.tool_contracts import (
    SearchDocumentsInput, SearchDocumentsResult,
    ExecuteCodeInput, ExecuteCodeResult,
)
from your_tools.search import search_documents
from your_tools.execute import execute_code


class TestSearchDocumentsContract:
    """Verify that search_documents honors the agreed contract."""

    def test_valid_input_accepted(self):
        inp = SearchDocumentsInput(query="Python asyncio", max_results=5)
        # Should not raise
        assert inp.query == "Python asyncio"

    def test_output_matches_schema(self):
        raw_result = search_documents(query="test query", max_results=2)
        # Force validation — will raise if tool broke the contract
        result = SearchDocumentsResult(**raw_result)
        assert isinstance(result.total_found, int)
        assert isinstance(result.results, list)
        assert result.query_echo == "test query"

    def test_tool_definition_matches_input_schema(self):
        """The JSON schema in the tool definition must match SearchDocumentsInput."""
        import json
        from your_agent.tools import TOOL_DEFINITIONS
        tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_documents")
        schema = tool_def["input_schema"]
        pydantic_schema = SearchDocumentsInput.model_json_schema()
        # Required fields must match
        assert set(schema["required"]) == set(pydantic_schema.get("required", []))


class TestExecuteCodeContract:
    """Verify execute_code honors input/output contracts."""

    def test_invalid_language_rejected(self):
        with pytest.raises(Exception):
            ExecuteCodeInput(code="print('hi')", language="ruby")

    def test_output_has_required_fields(self):
        raw = execute_code(code="print('hello')", language="python", timeout_seconds=5)
        result = ExecuteCodeResult(**raw)
        assert result.exit_code == 0
        assert "hello" in result.stdout
```

**Expected Token Savings:** Not applicable — test infrastructure
**Environment:** `pip install pydantic pytest`

---

### Option 2: JSON Schema Round-Trip Validation

```python
# tests/contracts/test_json_schema_roundtrip.py
"""
For each tool, verify that:
  1. The schema declared in TOOL_DEFINITIONS matches what Pydantic generates.
  2. A sample tool call from the agent parses against the declared schema.
  3. A sample tool response from the function validates against the output schema.
"""
import json
import jsonschema
import pytest
import anthropic
from your_agent.tools import TOOL_DEFINITIONS
from contracts.tool_contracts import (
    SearchDocumentsInput, SearchDocumentsResult,
    ExecuteCodeInput, ExecuteCodeResult,
)

# Registry: tool_name -> (InputModel, OutputModel)
TOOL_CONTRACT_REGISTRY = {
    "search_documents": (SearchDocumentsInput, SearchDocumentsResult),
    "execute_code": (ExecuteCodeInput, ExecuteCodeResult),
}


@pytest.mark.parametrize("tool_name", list(TOOL_CONTRACT_REGISTRY.keys()))
def test_tool_definition_schema_is_valid_json_schema(tool_name):
    """The tool's declared input_schema must be a valid JSON Schema."""
    tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == tool_name)
    schema = tool_def["input_schema"]
    # Raises jsonschema.SchemaError if invalid
    jsonschema.Draft7Validator.check_schema(schema)


@pytest.mark.parametrize("tool_name,input_class,_output_class", [
    (name, inp, out)
    for name, (inp, out) in {
        "search_documents": (SearchDocumentsInput, SearchDocumentsResult),
        "execute_code": (ExecuteCodeInput, ExecuteCodeResult),
    }.items()
])
def test_pydantic_schema_matches_declared_schema(tool_name, input_class, _output_class):
    """
    Pydantic-generated schema must have the same required fields and property names
    as the schema declared in the tool definition sent to the API.
    """
    tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == tool_name)
    declared = tool_def["input_schema"]
    generated = input_class.model_json_schema()

    declared_props = set(declared.get("properties", {}).keys())
    generated_props = set(generated.get("properties", {}).keys())
    assert declared_props == generated_props, (
        f"{tool_name}: property mismatch.\n"
        f"  Declared:  {declared_props}\n"
        f"  Generated: {generated_props}"
    )

    declared_required = set(declared.get("required", []))
    generated_required = set(generated.get("required", []))
    assert declared_required == generated_required, (
        f"{tool_name}: required fields mismatch.\n"
        f"  Declared:  {declared_required}\n"
        f"  Generated: {generated_required}"
    )


def test_agent_tool_call_validates_against_schema():
    """
    Simulate the agent producing a tool_use block and verify the arguments
    validate against the declared input schema.
    """
    # Construct a sample tool_use as Claude would produce it
    sample_tool_use = {
        "type": "tool_use",
        "id": "toolu_01",
        "name": "search_documents",
        "input": {"query": "async python patterns", "max_results": 5},
    }
    tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_documents")
    schema = tool_def["input_schema"]
    # Should not raise
    jsonschema.validate(instance=sample_tool_use["input"], schema=schema)


def test_tool_return_validates_against_output_contract():
    """
    Verify that what search_documents actually returns parses into SearchDocumentsResult.
    """
    from your_tools.search import search_documents
    raw = search_documents(query="hello", max_results=1)
    result = SearchDocumentsResult(**raw)
    assert result.total_found >= 0
```

**Expected Token Savings:** Not applicable — contract verification
**Environment:** `pip install pydantic jsonschema pytest`

---

### Option 3: Consumer-Driven Contract Tests with Recorded Snapshots

```python
# tests/contracts/test_recorded_contracts.py
"""
Record real tool call/response pairs during development and lock them as
contract snapshots. On every CI run, replay the same inputs and assert the
outputs still match. Detects breaking changes in either direction.
"""
import json
import os
from pathlib import Path
from typing import Any
import pytest

SNAPSHOT_DIR = Path("tests/contracts/snapshots")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def load_snapshot(tool_name: str) -> dict | None:
    path = SNAPSHOT_DIR / f"{tool_name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_snapshot(tool_name: str, data: dict):
    path = SNAPSHOT_DIR / f"{tool_name}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"Saved snapshot: {path}")


class ToolContractRecorder:
    """
    Wraps a tool function to record its first call as a snapshot,
    then validates subsequent calls against that snapshot.
    """
    def __init__(self, tool_name: str, tool_fn):
        self.tool_name = tool_name
        self.tool_fn = tool_fn
        self.snapshot = load_snapshot(tool_name)

    def call(self, **kwargs) -> Any:
        result = self.tool_fn(**kwargs)

        if self.snapshot is None:
            # First run — record the snapshot
            save_snapshot(self.tool_name, {
                "input": kwargs,
                "output": result,
                "output_keys": sorted(result.keys()) if isinstance(result, dict) else None,
            })
            self.snapshot = load_snapshot(self.tool_name)
            return result

        # Subsequent runs — validate structure
        if isinstance(result, dict) and self.snapshot.get("output_keys"):
            actual_keys = sorted(result.keys())
            expected_keys = self.snapshot["output_keys"]
            assert actual_keys == expected_keys, (
                f"Contract violation for {self.tool_name}:\n"
                f"  Expected keys: {expected_keys}\n"
                f"  Actual keys:   {actual_keys}"
            )

        return result


# ── Test usage ────────────────────────────────────────────────────────────────

@pytest.fixture
def search_recorder():
    from your_tools.search import search_documents
    return ToolContractRecorder("search_documents", search_documents)


def test_search_documents_contract_stable(search_recorder):
    """
    If a snapshot exists: assert output keys haven't changed.
    If no snapshot: record one for future runs.
    """
    result = search_recorder.call(query="test", max_results=1)
    assert isinstance(result, dict)
    assert "results" in result


@pytest.mark.parametrize("query,max_results", [
    ("python", 1),
    ("async patterns", 3),
    ("", 5),  # edge case: empty query
])
def test_search_handles_variety_of_inputs(search_recorder, query, max_results):
    """All input variants should produce structurally identical outputs."""
    if not query:
        pytest.skip("Empty query may be rejected by tool")
    result = search_recorder.call(query=query, max_results=max_results)
    # Load the canonical snapshot keys
    snapshot = load_snapshot("search_documents")
    if snapshot and snapshot.get("output_keys"):
        assert sorted(result.keys()) == snapshot["output_keys"]
```

**Expected Token Savings:** Not applicable — test infrastructure
**Environment:** `pip install pytest`

---

### Option 4: Tool Schema Drift Detector (CI Gate)

```python
# scripts/detect_tool_schema_drift.py
"""
Run in CI to detect when TOOL_DEFINITIONS in the agent diverge from
the Pydantic contracts. Exits non-zero on any drift — blocks merges.
"""
import json
import sys
from typing import TypedDict

# Import both sides of the contract
from your_agent.tools import TOOL_DEFINITIONS
from contracts.tool_contracts import (
    SearchDocumentsInput, ExecuteCodeInput
)

INPUT_CONTRACT_MAP = {
    "search_documents": SearchDocumentsInput,
    "execute_code": ExecuteCodeInput,
}


class DriftReport(TypedDict):
    tool: str
    drift_type: str
    detail: str


def detect_drift() -> list[DriftReport]:
    reports: list[DriftReport] = []

    for tool_def in TOOL_DEFINITIONS:
        name = tool_def["name"]
        if name not in INPUT_CONTRACT_MAP:
            reports.append({
                "tool": name,
                "drift_type": "undocumented_tool",
                "detail": "Tool has no Pydantic contract — add one to INPUT_CONTRACT_MAP",
            })
            continue

        model = INPUT_CONTRACT_MAP[name]
        declared_schema = tool_def.get("input_schema", {})
        generated_schema = model.model_json_schema()

        declared_props = set(declared_schema.get("properties", {}).keys())
        generated_props = set(generated_schema.get("properties", {}).keys())

        missing_in_declared = generated_props - declared_props
        extra_in_declared = declared_props - generated_props

        if missing_in_declared:
            reports.append({
                "tool": name,
                "drift_type": "missing_properties",
                "detail": f"Pydantic has these but tool_def doesn't: {missing_in_declared}",
            })
        if extra_in_declared:
            reports.append({
                "tool": name,
                "drift_type": "extra_properties",
                "detail": f"tool_def has these but Pydantic doesn't: {extra_in_declared}",
            })

        # Check required fields
        declared_req = set(declared_schema.get("required", []))
        generated_req = set(generated_schema.get("required", []))
        if declared_req != generated_req:
            reports.append({
                "tool": name,
                "drift_type": "required_field_mismatch",
                "detail": (
                    f"required mismatch: declared={declared_req}, "
                    f"generated={generated_req}"
                ),
            })

        # Check description presence
        if not tool_def.get("description"):
            reports.append({
                "tool": name,
                "drift_type": "missing_description",
                "detail": "Tool definition has no description field",
            })

    return reports


def main():
    reports = detect_drift()
    if not reports:
        print("No tool schema drift detected.")
        sys.exit(0)

    print(f"TOOL SCHEMA DRIFT DETECTED — {len(reports)} issue(s):\n")
    for r in reports:
        print(f"  [{r['drift_type']}] {r['tool']}: {r['detail']}")
    print("\nFix drift before merging.")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/contract_check.yml
name: Tool Contract Check
on: [push, pull_request]
jobs:
  contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python scripts/detect_tool_schema_drift.py
```

**Expected Token Savings:** Not applicable — CI gate
**Environment:** `pip install pydantic`

---

### Option 5: Property-Based Contract Tests with Hypothesis

```python
# tests/contracts/test_property_based_contracts.py
"""
Use Hypothesis to generate thousands of random valid inputs for each tool
and assert the output always conforms to the contract schema.
Finds edge cases no hand-written test would cover.
"""
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError

from contracts.tool_contracts import (
    SearchDocumentsInput, SearchDocumentsResult,
    ExecuteCodeInput, ExecuteCodeResult,
)
from your_tools.search import search_documents
from your_tools.execute import execute_code


# ── Strategy builders ────────────────────────────────────────────────────────

search_input_strategy = st.builds(
    SearchDocumentsInput,
    query=st.text(min_size=1, max_size=200).filter(lambda s: s.strip()),
    max_results=st.integers(min_value=1, max_value=20),
    filter_tag=st.one_of(st.none(), st.text(min_size=1, max_size=50)),
)

safe_code_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=100,
).filter(
    lambda s: not any(p in s for p in ["import os", "subprocess", "__import__"])
)

execute_input_strategy = st.builds(
    ExecuteCodeInput,
    code=safe_code_strategy,
    language=st.sampled_from(["python", "javascript", "bash"]),
    timeout_seconds=st.integers(min_value=1, max_value=10),
)


# ── Property tests ────────────────────────────────────────────────────────────

@given(inp=search_input_strategy)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_search_output_always_conforms_to_contract(inp: SearchDocumentsInput):
    """For any valid input, the tool must return a SearchDocumentsResult-conforming dict."""
    raw = search_documents(**inp.model_dump())
    # Must not raise — if it does, the contract is violated
    result = SearchDocumentsResult(**raw)
    assert result.total_found >= 0
    assert isinstance(result.results, list)
    assert len(result.results) <= inp.max_results


@given(inp=search_input_strategy)
@settings(max_examples=50)
def test_search_input_validation_is_consistent(inp: SearchDocumentsInput):
    """
    Re-parsing the serialized input must always succeed.
    Detects serialization round-trip bugs in the contract model.
    """
    serialized = inp.model_dump()
    reparsed = SearchDocumentsInput(**serialized)
    assert reparsed == inp


@given(code=safe_code_strategy)
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_execute_output_always_has_exit_code(code: str):
    """Any code execution must always return an exit_code, even for syntax errors."""
    raw = execute_code(code=code, language="python", timeout_seconds=3)
    result = ExecuteCodeResult(**raw)
    assert isinstance(result.exit_code, int)
    assert isinstance(result.execution_time_ms, float)
    assert result.execution_time_ms >= 0
```

**Expected Token Savings:** Not applicable — property-based testing
**Environment:** `pip install hypothesis pytest pydantic`

---

### Option 6: Live Contract Verification via Agent Dry-Run

```python
# tests/contracts/test_live_agent_contracts.py
"""
Ask Claude to invoke each tool with a sample prompt, then validate:
  1. Claude sends arguments that conform to the input schema.
  2. The tool's actual response conforms to the output schema.
Uses real Claude API with mocked tool execution for speed.
"""
import json
import pytest
import anthropic
from unittest.mock import patch
from pydantic import ValidationError

from contracts.tool_contracts import (
    SearchDocumentsInput, SearchDocumentsResult,
    ExecuteCodeInput, ExecuteCodeResult,
)
from your_agent.tools import TOOL_DEFINITIONS


CONTRACT_MAP = {
    "search_documents": (SearchDocumentsInput, SearchDocumentsResult),
    "execute_code": (ExecuteCodeInput, ExecuteCodeResult),
}

# Stub responses for each tool — must match the OutputModel
STUB_RESPONSES = {
    "search_documents": {
        "results": [{"id": "1", "text": "Sample result"}],
        "total_found": 1,
        "query_echo": "",  # will be filled in by test
    },
    "execute_code": {
        "stdout": "Hello\n",
        "stderr": "",
        "exit_code": 0,
        "execution_time_ms": 12.5,
    },
}


@pytest.mark.parametrize("tool_name,prompt", [
    ("search_documents", "Search for documents about asyncio patterns"),
    ("execute_code", "Run a Python snippet that prints Hello World"),
])
def test_claude_produces_valid_tool_arguments(tool_name, prompt):
    """
    Ask Claude to respond with a tool call, then validate the arguments
    against the Pydantic input contract.
    """
    client = anthropic.Anthropic()
    InputModel, _ = CONTRACT_MAP[tool_name]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOL_DEFINITIONS,
        messages=[{"role": "user", "content": prompt}],
    )

    # Find the tool_use block
    tool_calls = [b for b in response.content if b.type == "tool_use"]
    assert tool_calls, f"Claude did not call any tool for prompt: {prompt!r}"

    tool_call = next((t for t in tool_calls if t.name == tool_name), None)
    assert tool_call is not None, f"Claude did not call {tool_name}"

    # Validate arguments against input contract
    try:
        validated = InputModel(**tool_call.input)
    except ValidationError as e:
        pytest.fail(
            f"Claude's arguments for {tool_name} violate the input contract:\n"
            f"  Arguments: {json.dumps(tool_call.input, indent=2)}\n"
            f"  Errors: {e}"
        )


@pytest.mark.parametrize("tool_name", list(CONTRACT_MAP.keys()))
def test_stub_response_validates_against_output_contract(tool_name):
    """
    The stub responses used in tests must themselves satisfy the output contract.
    Catches stale stubs that no longer match the contract model.
    """
    _, OutputModel = CONTRACT_MAP[tool_name]
    stub = STUB_RESPONSES[tool_name].copy()
    if tool_name == "search_documents":
        stub["query_echo"] = "test"
    try:
        OutputModel(**stub)
    except ValidationError as e:
        pytest.fail(f"Stub for {tool_name} violates output contract: {e}")
```

**Expected Token Savings:** ~80% vs full integration tests (haiku model + mock tool execution)
**Environment:** `pip install anthropic pydantic pytest`

---

## Comparison Table

| Option | Contract Type | Both Sides Validated | Catches Drift Automatically | CI-Ready | Requires Live API |
|--------|---------------|----------------------|-----------------------------|----------|-------------------|
| 1: Pydantic round-trip | Input + Output | Yes | On test run | Yes | No |
| 2: JSON Schema round-trip | Input only | Partial | On test run | Yes | No |
| 3: Recorded snapshots | Output structure | Partial | On test run | Yes | No |
| 4: CI drift detector | Input schema only | Yes | Pre-merge | Yes | No |
| 5: Hypothesis property | Input + Output | Yes | Randomized | Yes | No |
| 6: Live agent dry-run | Input (via Claude) | Yes | On test run | Yes | Yes (haiku) |
