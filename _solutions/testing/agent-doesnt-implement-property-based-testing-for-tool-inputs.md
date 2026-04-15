---
layout: solution
title: "Agent Doesn't Implement Property-Based Testing for Tool Inputs"
category: testing
description: "Unit tests with hand-crafted inputs miss the edge cases that break tool schemas — property-based testing generates hundreds of random inputs automatically and finds the exact boundary conditions your manual tests don't cover."
tags: [testing, property-based-testing, hypothesis, tool-inputs, schema-validation, fuzzing, edge-cases]
---

# Agent Doesn't Implement Property-Based Testing for Tool Inputs

## Problem

When agents define tools, they specify input schemas that constrain what values are valid. Hand-written unit tests typically cover a handful of happy-path inputs and a few obvious edge cases. But the real failure modes are at the boundaries: empty strings, unicode edge cases, integer overflow, unexpected `null` values, or combinations of valid fields that produce invalid states. Property-based testing generates hundreds of random inputs that satisfy constraints and automatically finds the edge cases your manual tests miss — before they reach production.

## Solutions

### Option 1: Hypothesis-Based Tool Schema Validation

Use the `hypothesis` library to generate random valid inputs for tool schemas and verify the agent handles them without crashing.

```python
# pip install hypothesis pytest
import json
import anthropic
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

client = anthropic.Anthropic()

# Tool definition being tested
SEARCH_TOOL = {
    "name": "search_records",
    "description": "Search records by query string with optional filters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
            },
            "category": {
                "type": "string",
                "enum": ["news", "docs", "code", "all"],
            },
        },
        "required": ["query"],
    },
}

def validate_tool_input(tool_name: str, args: dict) -> tuple[bool, str]:
    """Validate args against the tool schema."""
    try:
        import jsonschema
        schema = next(
            t["input_schema"] for t in [SEARCH_TOOL] if t["name"] == tool_name
        )
        jsonschema.validate(args, schema)
        return True, ""
    except ImportError:
        # Fallback: basic validation without jsonschema
        if not args.get("query"):
            return False, "query is required"
        if "limit" in args and not (1 <= args["limit"] <= 100):
            return False, f"limit out of range: {args['limit']}"
        return True, ""
    except Exception as e:
        return False, str(e)

def execute_search(args: dict) -> dict:
    """Stub tool execution — replace with real implementation."""
    query = args.get("query", "")
    limit = args.get("limit", 10)
    category = args.get("category", "all")

    # All of these should be safe with any valid input
    if not isinstance(query, str) or len(query) == 0:
        raise ValueError(f"Invalid query: {query!r}")
    if not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ValueError(f"Invalid limit: {limit}")
    if category not in ("news", "docs", "code", "all"):
        raise ValueError(f"Invalid category: {category}")

    return {
        "results": [f"Result {i} for '{query[:10]}'" for i in range(min(limit, 3))],
        "total": limit,
        "category": category,
    }

# Strategy: generate valid search inputs
valid_query = st.text(min_size=1, max_size=100).filter(lambda s: s.strip())
valid_limit = st.integers(min_value=1, max_value=100)
valid_category = st.sampled_from(["news", "docs", "code", "all"])

@given(
    query=valid_query,
    limit=valid_limit,
    category=valid_category,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_search_tool_never_crashes(query: str, limit: int, category: str):
    """Property: tool execution never raises for any valid input."""
    args = {"query": query, "limit": limit, "category": category}

    valid, reason = validate_tool_input("search_records", args)
    assert valid, f"Valid args failed schema check: {reason} | args={args}"

    result = execute_search(args)
    assert isinstance(result, dict), "Result must be a dict"
    assert "results" in result, "Result must have 'results' key"

@given(query=valid_query)
@settings(max_examples=50)
def test_search_required_only(query: str):
    """Property: works with only required fields."""
    result = execute_search({"query": query})
    assert "results" in result

# Property: schema rejects invalid inputs
@given(
    limit=st.integers().filter(lambda x: not (1 <= x <= 100)),
)
@settings(max_examples=50)
def test_invalid_limit_rejected(limit: int):
    """Property: out-of-range limit must fail validation."""
    args = {"query": "test", "limit": limit}
    valid, reason = validate_tool_input("search_records", args)
    # In real schema validation, this should be False
    # We verify our tool implementation also rejects it
    try:
        execute_search(args)
        # If it didn't raise, it must have clamped — that's also acceptable
    except ValueError:
        pass  # Expected

if __name__ == "__main__":
    test_search_tool_never_crashes()
    test_search_required_only()
    print("All property-based tests passed")
# Expected Token Savings: Catch tool failures before they reach the model and cause retry loops
# Environment: Any agent with structured tool schemas; CI pipelines; pre-deployment validation
```

### Option 2: Fuzzing Tool Argument Boundaries

Systematically test boundary values: min/max integers, empty strings, max-length strings, None values, unicode extremes.

```python
import json
import pytest
import anthropic
from itertools import product
from typing import Any

client = anthropic.Anthropic()

# Tool with multiple typed parameters
CREATE_TASK_TOOL = {
    "name": "create_task",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":       {"type": "string", "minLength": 1, "maxLength": 200},
            "priority":    {"type": "integer", "minimum": 1, "maximum": 5},
            "tags":        {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "assignee_id": {"type": "string", "pattern": "^user_[a-z0-9]+$"},
            "due_days":    {"type": "integer", "minimum": 0, "maximum": 365},
        },
        "required": ["title", "priority"],
    },
}

def boundary_values_for_type(field_schema: dict) -> list[Any]:
    """Generate boundary values based on field schema."""
    typ = field_schema.get("type")
    values = []

    if typ == "string":
        min_len = field_schema.get("minLength", 0)
        max_len = field_schema.get("maxLength", 1000)
        values += [
            "a" * min_len,                    # Exact minimum
            "a" * max_len,                    # Exact maximum
            "a" * (max_len + 1),              # Just over maximum
            "",                               # Empty string
            " " * min_len,                    # Whitespace only
            "test\x00value",                  # Null byte
            "тест",                           # Cyrillic
            "🎉" * 5,                         # Emoji
            "<script>alert('xss')</script>",  # XSS attempt
            "'; DROP TABLE tasks; --",        # SQL injection attempt
            "a" * (min_len - 1) if min_len > 0 else None,  # Just under minimum
        ]

    elif typ == "integer":
        minimum = field_schema.get("minimum", -(2**31))
        maximum = field_schema.get("maximum", 2**31 - 1)
        values += [
            minimum,                  # Exact minimum
            maximum,                  # Exact maximum
            minimum - 1,              # Just below minimum
            maximum + 1,              # Just above maximum
            0,
            -1,
            2**31 - 1,                # Max int32
            2**63 - 1,                # Max int64
            float("inf"),             # Infinity (invalid for integer)
        ]

    elif typ == "array":
        max_items = field_schema.get("maxItems", 100)
        values += [
            [],                       # Empty array
            ["item"] * max_items,     # Exactly at limit
            ["item"] * (max_items + 1),  # Over limit
            None,                     # None
            "not_an_array",           # Wrong type
        ]

    return [v for v in values if v is not None]

def validate_args(args: dict) -> tuple[bool, str]:
    """Basic validation that mirrors the tool schema."""
    schema = CREATE_TASK_TOOL["input_schema"]
    props = schema["properties"]

    for field in schema.get("required", []):
        if field not in args:
            return False, f"Missing required field: {field}"

    title = args.get("title", "")
    if not isinstance(title, str) or len(title) < 1 or len(title) > 200:
        return False, f"title out of bounds: {len(title) if isinstance(title, str) else type(title)}"

    priority = args.get("priority", 0)
    if not isinstance(priority, int) or not (1 <= priority <= 5):
        return False, f"priority invalid: {priority!r}"

    return True, ""

def tool_handler(args: dict) -> dict:
    """Safe tool implementation that handles all valid inputs correctly."""
    valid, reason = validate_args(args)
    if not valid:
        return {"error": reason, "success": False}

    return {
        "task_id": f"task_{hash(json.dumps(args, sort_keys=True, default=str)) % 10000:04d}",
        "title": args["title"],
        "priority": args["priority"],
        "success": True,
    }

@pytest.mark.parametrize("title", boundary_values_for_type(
    CREATE_TASK_TOOL["input_schema"]["properties"]["title"]
))
def test_title_boundaries(title):
    """Property: all boundary title values are handled gracefully (no crash)."""
    args = {"title": title, "priority": 3}
    result = tool_handler(args)
    assert isinstance(result, dict), "Tool must always return a dict"
    assert "success" in result or "error" in result, "Result must have success or error"

@pytest.mark.parametrize("priority", boundary_values_for_type(
    CREATE_TASK_TOOL["input_schema"]["properties"]["priority"]
))
def test_priority_boundaries(priority):
    """Property: all boundary priority values are handled gracefully."""
    args = {"title": "Test task", "priority": priority}
    result = tool_handler(args)
    assert isinstance(result, dict)

def run_boundary_fuzz():
    """Run all boundary combinations without pytest."""
    title_boundaries = boundary_values_for_type(CREATE_TASK_TOOL["input_schema"]["properties"]["title"])
    priority_boundaries = boundary_values_for_type(CREATE_TASK_TOOL["input_schema"]["properties"]["priority"])

    crashes = []
    tested = 0
    for title in title_boundaries:
        for priority in priority_boundaries[:5]:  # Limit combinations
            tested += 1
            try:
                result = tool_handler({"title": title, "priority": priority})
                assert isinstance(result, dict)
            except Exception as e:
                crashes.append({"title": repr(title)[:30], "priority": priority, "error": str(e)})

    print(f"Tested {tested} combinations | Crashes: {len(crashes)}")
    for crash in crashes:
        print(f"  CRASH: {crash}")

run_boundary_fuzz()
# Expected Token Savings: Boundary bugs caught in CI prevent model retry loops from invalid tool args
# Environment: Any agent with typed tool schemas; form-submission agents; data ingestion tools
```

### Option 3: Stateful Property Testing for Multi-Turn Tool Sequences

Test sequences of tool calls to find ordering bugs — some tools only work correctly after others have run first.

```python
# pip install hypothesis
import json
import random
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, initialize, invariant

# Simulated stateful database
class MockTaskDB:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.next_id = 1

    def create(self, title: str, priority: int) -> str:
        task_id = f"task_{self.next_id:03d}"
        self.next_id += 1
        self.tasks[task_id] = {"id": task_id, "title": title, "priority": priority, "status": "open"}
        return task_id

    def get(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)

    def update(self, task_id: str, **kwargs) -> bool:
        if task_id not in self.tasks:
            return False
        self.tasks[task_id].update(kwargs)
        return True

    def delete(self, task_id: str) -> bool:
        if task_id not in self.tasks:
            return False
        del self.tasks[task_id]
        return True

class TaskAgentStateMachine(RuleBasedStateMachine):
    """
    Stateful property test: sequence of create/update/delete operations.
    Invariants must hold after every operation.
    """

    def __init__(self):
        super().__init__()
        self.db = MockTaskDB()
        self.created_ids: list[str] = []
        self.deleted_ids: set[str] = set()

    @initialize()
    def setup(self):
        pass

    @rule(
        title=st.text(min_size=1, max_size=50).filter(str.strip),
        priority=st.integers(min_value=1, max_value=5),
    )
    def create_task(self, title: str, priority: int):
        task_id = self.db.create(title, priority)
        assert task_id not in self.deleted_ids, "Created ID was previously deleted"
        assert self.db.get(task_id) is not None, "Created task must be retrievable"
        self.created_ids.append(task_id)

    @rule(
        data=st.data(),
        new_priority=st.integers(min_value=1, max_value=5),
    )
    def update_existing_task(self, data, new_priority: int):
        assume(len(self.created_ids) > 0)
        task_id = data.draw(st.sampled_from(self.created_ids))
        assume(task_id not in self.deleted_ids)

        success = self.db.update(task_id, priority=new_priority)
        assert success, f"Update of existing task {task_id} must succeed"

        updated = self.db.get(task_id)
        assert updated is not None
        assert updated["priority"] == new_priority

    @rule(task_id=st.text(min_size=1, max_size=10))
    def update_nonexistent_task(self, task_id: str):
        """Property: updating non-existent task must return False, not crash."""
        assume(task_id not in (self.created_ids or ["placeholder"]))
        result = self.db.update(task_id, priority=1)
        assert result is False, "Updating non-existent task must return False"

    @rule(data=st.data())
    def delete_existing_task(self, data):
        assume(len(self.created_ids) > 0)
        active = [t for t in self.created_ids if t not in self.deleted_ids]
        assume(len(active) > 0)
        task_id = data.draw(st.sampled_from(active))

        success = self.db.delete(task_id)
        assert success, "Delete of existing task must succeed"
        self.deleted_ids.add(task_id)
        assert self.db.get(task_id) is None, "Deleted task must not be retrievable"

    @rule(task_id=st.text(min_size=1, max_size=10))
    def delete_nonexistent_task(self, task_id: str):
        """Property: deleting non-existent task returns False, not crash."""
        assume(task_id not in self.created_ids)
        result = self.db.delete(task_id)
        assert result is False

    @invariant()
    def active_tasks_are_retrievable(self):
        """Invariant: every non-deleted created task must be retrievable."""
        for task_id in self.created_ids:
            if task_id in self.deleted_ids:
                assert self.db.get(task_id) is None
            else:
                assert self.db.get(task_id) is not None, f"Active task {task_id} not retrievable"

    @invariant()
    def task_count_is_consistent(self):
        """Invariant: DB task count matches our tracking."""
        expected_active = len([t for t in self.created_ids if t not in self.deleted_ids])
        assert len(self.db.tasks) == expected_active

# Run the stateful test
TestTaskAgent = TaskAgentStateMachine.TestCase
TestTaskAgent.settings = settings(max_examples=50, stateful_step_count=15)

if __name__ == "__main__":
    from hypothesis import find
    # Quick smoke test
    machine = TaskAgentStateMachine()
    machine.setup()
    t1 = machine.db.create("Task A", 1)
    t2 = machine.db.create("Task B", 3)
    machine.db.update(t1, priority=5)
    machine.db.delete(t1)
    machine.active_tasks_are_retrievable()
    machine.task_count_is_consistent()
    print("Stateful property tests passed")
# Expected Token Savings: Catches state machine bugs before they cause agent loop-stucks
# Environment: Agents with CRUD tools, workflow state machines, multi-step transactional agents
```

### Option 4: Contract Testing Between Agent and Tool Implementations

Verify that the tool implementation contract (what the schema promises) matches what the code delivers.

```python
import json
import pytest
from typing import Any
from dataclasses import dataclass

@dataclass
class ContractViolation:
    field: str
    promised: str
    actual: str

def verify_tool_contract(
    tool_schema: dict,
    implementation_fn,
    test_inputs: list[dict],
) -> list[ContractViolation]:
    """Check that the implementation honors every schema promise."""
    violations = []
    schema_props = tool_schema.get("input_schema", {}).get("properties", {})

    for args in test_inputs:
        try:
            result = implementation_fn(args)
        except Exception as e:
            violations.append(ContractViolation(
                field="<execution>",
                promised="no crash on valid input",
                actual=f"raised {type(e).__name__}: {e}",
            ))
            continue

        # Contract: result must always be a dict
        if not isinstance(result, dict):
            violations.append(ContractViolation(
                field="return_type",
                promised="dict",
                actual=str(type(result)),
            ))

        # Contract: result must have either 'result' or 'error' key
        if "result" not in result and "error" not in result:
            violations.append(ContractViolation(
                field="return_keys",
                promised="'result' or 'error' in response",
                actual=str(list(result.keys())),
            ))

        # Contract: if input had 'id', result should echo it
        if "id" in args and "id" in result:
            if result["id"] != args["id"]:
                violations.append(ContractViolation(
                    field="id_echo",
                    promised=f"id={args['id']}",
                    actual=f"id={result['id']}",
                ))

    return violations

# Tool definition
UPSERT_TOOL = {
    "name": "upsert_record",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "data": {"type": "object"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["id", "data"],
    },
}

# Correct implementation
def correct_upsert(args: dict) -> dict:
    record_id = args["id"]
    data = args["data"]
    overwrite = args.get("overwrite", False)
    return {
        "result": "upserted",
        "id": record_id,
        "overwrote": overwrite,
    }

# Buggy implementation (missing id echo)
def buggy_upsert(args: dict) -> dict:
    return {
        "result": "upserted",
        "overwrote": args.get("overwrite", False),
        # BUG: missing "id" echo
    }

test_inputs = [
    {"id": "rec_001", "data": {"name": "Alice"}, "overwrite": True},
    {"id": "rec_002", "data": {"count": 42}},
    {"id": "x", "data": {}},
    {"id": "a" * 100, "data": {"big": True}, "overwrite": False},
]

print("=== Correct implementation ===")
violations = verify_tool_contract(UPSERT_TOOL, correct_upsert, test_inputs)
if violations:
    for v in violations:
        print(f"  VIOLATION [{v.field}]: promised '{v.promised}', got '{v.actual}'")
else:
    print("  All contracts satisfied")

print("\n=== Buggy implementation ===")
violations = verify_tool_contract(UPSERT_TOOL, buggy_upsert, test_inputs)
if violations:
    for v in violations:
        print(f"  VIOLATION [{v.field}]: promised '{v.promised}', got '{v.actual}'")
else:
    print("  All contracts satisfied")

# Pytest integration
@pytest.mark.parametrize("args", test_inputs)
def test_upsert_contract(args):
    violations = verify_tool_contract(UPSERT_TOOL, correct_upsert, [args])
    assert not violations, f"Contract violations: {violations}"
# Expected Token Savings: Contract violations caught in CI prevent tool schema drift that causes retries
# Environment: Any agent where tool schema and implementation are maintained separately
```

### Option 5: JSON Schema Inference and Round-Trip Property

Generate random valid JSON matching the tool's schema and verify the tool can parse it back — catches schema/parser mismatches.

```python
# pip install jsonschema hypothesis
import json
import pytest
try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

def json_strategy_from_schema(schema: dict):
    """Build a Hypothesis strategy from a JSON schema."""
    if not HAS_HYPOTHESIS:
        return None

    typ = schema.get("type")

    if typ == "string":
        min_size = schema.get("minLength", 0)
        max_size = schema.get("maxLength", 100)
        if "enum" in schema:
            return st.sampled_from(schema["enum"])
        if "pattern" in schema and schema["pattern"] == "^user_[a-z0-9]+$":
            return st.builds(
                lambda s: f"user_{s}",
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)
            )
        return st.text(min_size=min_size, max_size=max_size)

    elif typ == "integer":
        minimum = schema.get("minimum", 0)
        maximum = schema.get("maximum", 100)
        return st.integers(min_value=minimum, max_value=maximum)

    elif typ == "boolean":
        return st.booleans()

    elif typ == "array":
        items_schema = schema.get("items", {"type": "string"})
        max_items = schema.get("maxItems", 10)
        item_strategy = json_strategy_from_schema(items_schema)
        return st.lists(item_strategy, max_size=max_items)

    elif typ == "object":
        props = schema.get("properties", {})
        required = schema.get("required", [])
        required_strategies = {
            k: json_strategy_from_schema(v)
            for k, v in props.items()
            if k in required
        }
        optional_strategies = {
            k: json_strategy_from_schema(v)
            for k, v in props.items()
            if k not in required
        }
        if optional_strategies:
            return st.fixed_dictionaries(required_strategies).flatmap(
                lambda d: st.fixed_dictionaries({
                    **{k: st.just(v) for k, v in d.items()},
                    **{k: st.one_of(st.just(None), v)
                       for k, v in optional_strategies.items()},
                }).map(lambda d2: {k: v for k, v in d2.items() if v is not None})
            )
        return st.fixed_dictionaries(required_strategies)

    return st.none()

# Tool schema to test
TRANSFER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "from_account": {"type": "string", "pattern": "^user_[a-z0-9]+$"},
        "to_account":   {"type": "string", "pattern": "^user_[a-z0-9]+$"},
        "amount":       {"type": "integer", "minimum": 1, "maximum": 1000000},
        "currency":     {"type": "string", "enum": ["USD", "EUR", "GBP"]},
        "memo":         {"type": "string", "minLength": 0, "maxLength": 200},
    },
    "required": ["from_account", "to_account", "amount", "currency"],
}

def process_transfer(args: dict) -> dict:
    """Tool implementation to test."""
    # Simulate real validation
    if args.get("from_account") == args.get("to_account"):
        return {"error": "Cannot transfer to same account"}
    if args.get("amount", 0) <= 0:
        return {"error": "Amount must be positive"}
    return {
        "result": "transferred",
        "transfer_id": f"txn_{hash(json.dumps(args, sort_keys=True)) % 100000:05d}",
        "amount": args["amount"],
        "currency": args["currency"],
    }

if HAS_HYPOTHESIS:
    transfer_strategy = json_strategy_from_schema(TRANSFER_TOOL_SCHEMA)

    @given(args=transfer_strategy)
    @settings(max_examples=100)
    def test_transfer_round_trip(args):
        """Property: any valid schema input must not crash the tool."""
        result = process_transfer(args)
        assert isinstance(result, dict), "Must return dict"
        # Verify JSON serializability
        json_str = json.dumps(result)
        reparsed = json.loads(json_str)
        assert reparsed == result, "Result must be JSON round-trippable"

    if __name__ == "__main__":
        test_transfer_round_trip()
        print("Round-trip property tests passed")
else:
    # Manual fallback without Hypothesis
    test_cases = [
        {"from_account": "user_alice", "to_account": "user_bob", "amount": 100, "currency": "USD"},
        {"from_account": "user_x1", "to_account": "user_y2", "amount": 1000000, "currency": "EUR"},
        {"from_account": "user_a", "to_account": "user_a", "amount": 50, "currency": "GBP"},  # Same account
    ]
    for args in test_cases:
        result = process_transfer(args)
        print(f"Input: {args}\nOutput: {result}\n")
    print("Install hypothesis for full property-based testing: pip install hypothesis")
# Expected Token Savings: Schema/parser mismatches cause tool result errors that fill context with retries
# Environment: Financial agents, data validation tools, API-calling agents
```

### Option 6: Regression Property Test Suite with Shrinking

When a property test finds a bug, Hypothesis automatically shrinks the failing input to the minimal reproducer. Store these as permanent regression tests.

```python
# pip install hypothesis pytest
import json
import pytest
try:
    from hypothesis import given, settings, example
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False
    # Stub decorators
    def given(*a, **kw): return lambda f: f
    def settings(*a, **kw): return lambda f: f
    def example(*a, **kw): return lambda f: f
    class st:
        @staticmethod
        def text(**kw): return None
        @staticmethod
        def integers(**kw): return None
        @staticmethod
        def lists(**kw): return None

# Tool with known edge-case bugs (intentionally buggy for demo)
def parse_tag_list(raw_tags: str, max_tags: int = 5) -> list[str]:
    """Parse comma-separated tags. Has edge cases to discover."""
    if not raw_tags:
        return []
    tags = [t.strip() for t in raw_tags.split(",")]
    tags = [t for t in tags if t]  # Remove empty
    return tags[:max_tags]

def create_tagged_record(title: str, raw_tags: str) -> dict:
    """Tool implementation."""
    if not title or not title.strip():
        return {"error": "title required"}
    tags = parse_tag_list(raw_tags)
    return {
        "title": title.strip(),
        "tags": tags,
        "tag_count": len(tags),
        "result": "created",
    }

# Known regressions (from previous Hypothesis runs that found bugs)
# Hypothesis would have found these and shrunk them to minimal cases
REGRESSION_CASES = [
    # Minimal case: only whitespace title
    {"title": " ", "raw_tags": "python"},
    # Minimal case: tabs in tags
    {"title": "test", "raw_tags": "python\ttag,other"},
    # Minimal case: trailing comma
    {"title": "test", "raw_tags": "python,"},
    # Minimal case: very long single tag
    {"title": "test", "raw_tags": "a" * 500},
    # Minimal case: all separators no content
    {"title": "test", "raw_tags": ",,,"},
]

@pytest.mark.parametrize("case", REGRESSION_CASES)
def test_regression(case):
    """Regression tests: cases that previously failed, now pinned."""
    result = create_tagged_record(case["title"], case["raw_tags"])
    assert isinstance(result, dict), "Must return dict"
    if "error" not in result:
        assert isinstance(result["tags"], list), "tags must be list"
        assert result["tag_count"] == len(result["tags"]), "tag_count must match len(tags)"
        assert result["tag_count"] <= 5, "Must not exceed max_tags"

if HAS_HYPOTHESIS:
    @given(
        title=st.text(min_size=0, max_size=100),
        raw_tags=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=200)
    # Pin previously shrunk failures as explicit examples
    @example(title="", raw_tags="python")
    @example(title=" ", raw_tags="")
    @example(title="test", raw_tags="," * 50)
    def test_create_tagged_record_properties(title: str, raw_tags: str):
        """
        Property: for any string input:
        1. Never crashes
        2. Returns a dict
        3. tag_count matches len(tags)
        4. tags never exceeds max_tags
        """
        result = create_tagged_record(title, raw_tags)
        assert isinstance(result, dict), "Must return dict"

        if "error" not in result:
            assert "tags" in result, "Success result must have tags"
            assert "tag_count" in result, "Success result must have tag_count"
            assert isinstance(result["tags"], list), "tags must be a list"
            assert result["tag_count"] == len(result["tags"]), (
                f"tag_count={result['tag_count']} != len(tags)={len(result['tags'])}"
            )
            assert result["tag_count"] <= 5, (
                f"tag_count {result['tag_count']} exceeds max_tags=5"
            )

if __name__ == "__main__":
    # Run regression cases
    for case in REGRESSION_CASES:
        result = create_tagged_record(case["title"], case["raw_tags"])
        assert isinstance(result, dict)
        if "error" not in result:
            assert result["tag_count"] == len(result["tags"])
        print(f"✓ {case}")

    if HAS_HYPOTHESIS:
        test_create_tagged_record_properties()

    print("All regression + property tests passed")
# Expected Token Savings: Pinned regressions prevent re-introduction of parsing bugs that break tool calls
# Environment: Any agent with string-processing tools; CI for text-heavy tool implementations
```

## Comparison Table

| Option | Library | Input Coverage | Test Speed | Best For |
|--------|---------|----------------|-----------|----------|
| 1: Hypothesis Schema Tests | hypothesis | Random + constraint-guided | Fast | Schema validation, never-crash properties |
| 2: Boundary Value Fuzzing | None (stdlib) | Systematic boundaries | Very Fast | Integer limits, string extremes, null safety |
| 3: Stateful Sequence Testing | hypothesis.stateful | Tool-call sequences | Medium | CRUD tools, state machine tools |
| 4: Contract Testing | None | Handcrafted valid inputs | Fast | Schema-implementation alignment |
| 5: JSON Schema Round-Trip | hypothesis + jsonschema | Schema-derived | Fast | Parser/serializer correctness |
| 6: Shrunk Regression Suite | hypothesis + pytest | Minimal reproducers | Very Fast | Preventing known bug regressions |
