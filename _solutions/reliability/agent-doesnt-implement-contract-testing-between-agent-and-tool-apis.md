---
title: "Agent Doesn't Implement Contract Testing Between Agent and Tool APIs"
description: "How to use consumer-driven contract testing to verify that tool API schemas, response formats, and behavior contracts remain compatible with agent expectations — catching breaking changes before they reach production."
date: 2025-01-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-contract-testing-between-agent-and-tool-apis
tags:
  - reliability
  - contract-testing
  - api-compatibility
  - tool-apis
  - schema-validation
  - breaking-changes
  - ci-cd
symptoms:
  - "Tool API response shape changes silently break the agent's parsing logic"
  - "New required fields added to tool inputs cause agent failures without clear errors"
  - "No automated test catches when a tool returns a new enum value the agent doesn't handle"
  - "Agent behavior changes after a tool library upgrade go undetected until production"
  - "Cannot confidently upgrade tool dependencies without running full end-to-end tests"
  - "Tool teams and agent teams have no shared understanding of their API contract"
---

## Why This Happens

Agents depend on tools that evolve independently. A tool provider adds a new required field, renames a response property, or changes an enum's possible values. The agent, unaware of the change, continues calling the tool with the old schema and silently receives malformed data — or fails with a cryptic error. Without explicit contracts and automated contract verification, API drift between agent and tool goes undetected until runtime.

Consumer-Driven Contract Testing (CDCT) inverts the traditional approach: the consumer (agent) writes the contract it expects from the provider (tool), and the provider runs tests against that contract on every deploy. Neither side needs to coordinate a release or run the other's test suite — the contract acts as a shared interface specification that is automatically validated from both sides.

---

## Solution 1: Tool Contract Definition

Define the agent's expectations of each tool as a structured contract — input schema, output schema, and invariants.

```python
from dataclasses import dataclass, field
from typing import Any, Optional
import json
import jsonschema

@dataclass
class FieldContract:
    name: str
    type: str           # "string", "integer", "number", "boolean", "array", "object", "null"
    required: bool = True
    nullable: bool = False
    enum_values: list[Any] = field(default_factory=list)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    pattern: Optional[str] = None
    description: str = ""

@dataclass
class ToolContract:
    """
    Defines the agent's expectations of a single tool's input and output schemas.
    The agent is the consumer; the tool implementation is the provider.
    """
    tool_name: str
    version: str
    consumer: str     # Name of the agent that owns this contract
    provider: str     # Name of the tool/service

    # Input contract: what the agent will send
    input_fields: list[FieldContract] = field(default_factory=list)
    # Output contract: what the agent expects to receive
    output_fields: list[FieldContract] = field(default_factory=list)
    # Behavioral invariants
    invariants: list[str] = field(default_factory=list)

    def to_json_schema_input(self) -> dict:
        return self._fields_to_schema(self.input_fields)

    def to_json_schema_output(self) -> dict:
        return self._fields_to_schema(self.output_fields)

    def _fields_to_schema(self, fields: list[FieldContract]) -> dict:
        properties = {}
        required = []
        for f in fields:
            prop: dict = {"type": f.type}
            if f.nullable:
                prop["type"] = [f.type, "null"]
            if f.enum_values:
                prop["enum"] = f.enum_values
            if f.min_value is not None:
                prop["minimum"] = f.min_value
            if f.max_value is not None:
                prop["maximum"] = f.max_value
            if f.pattern:
                prop["pattern"] = f.pattern
            if f.description:
                prop["description"] = f.description
            properties[f.name] = prop
            if f.required:
                required.append(f.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": True,  # Consumer contract: don't break on extra fields
        }

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "version": self.version,
            "consumer": self.consumer,
            "provider": self.provider,
            "input_schema": self.to_json_schema_input(),
            "output_schema": self.to_json_schema_output(),
            "invariants": self.invariants,
        }

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ToolContract":
        with open(path) as f:
            data = json.load(f)
        # Reconstruct (simplified — full implementation would rebuild FieldContracts)
        return cls(
            tool_name=data["tool_name"],
            version=data["version"],
            consumer=data["consumer"],
            provider=data["provider"],
        )
```

---

## Solution 2: Consumer-Side Contract Verifier

Validate that a tool's actual response matches the contract the agent defined.

```python
import jsonschema
from dataclasses import dataclass

@dataclass
class ContractViolation:
    field: str
    expected: str
    actual: str
    severity: str  # "breaking", "warning"

@dataclass
class ContractVerificationResult:
    tool_name: str
    passed: bool
    violations: list[ContractViolation]
    message: str = ""

class ConsumerContractVerifier:
    """
    Validates that tool responses conform to the contract the agent expects.
    Used on the consumer (agent) side during integration testing.
    """

    def __init__(self, contract: ToolContract):
        self.contract = contract
        self._input_validator = jsonschema.Draft7Validator(contract.to_json_schema_input())
        self._output_validator = jsonschema.Draft7Validator(contract.to_json_schema_output())

    def verify_input(self, input_data: dict) -> ContractVerificationResult:
        """Verify that the agent's tool call input is contract-compliant."""
        violations = self._collect_violations(self._input_validator, input_data)
        return ContractVerificationResult(
            tool_name=self.contract.tool_name,
            passed=len(violations) == 0,
            violations=violations,
            message="Input validation failed" if violations else "Input valid",
        )

    def verify_output(self, output_data: dict) -> ContractVerificationResult:
        """Verify that the tool's response matches the agent's expected output contract."""
        violations = self._collect_violations(self._output_validator, output_data)
        return ContractVerificationResult(
            tool_name=self.contract.tool_name,
            passed=len(violations) == 0,
            violations=violations,
            message="Output contract violated" if violations else "Output valid",
        )

    def _collect_violations(
        self, validator: jsonschema.Validator, data: dict
    ) -> list[ContractViolation]:
        violations = []
        for error in validator.iter_errors(data):
            path = ".".join(str(p) for p in error.absolute_path) or "root"
            severity = "breaking" if "required" in error.validator else "warning"
            violations.append(ContractViolation(
                field=path,
                expected=str(error.schema),
                actual=str(error.instance)[:100],
                severity=severity,
            ))
        return violations

    def verify_invariants(self, output_data: dict) -> list[str]:
        """Check behavioral invariants (custom assertions beyond schema)."""
        failures = []
        for invariant_desc in self.contract.invariants:
            # Invariants are stored as descriptions — real implementation would have
            # callable predicates or expression evaluators
            pass
        return failures


# --- Example: define and verify a search tool contract ---

def create_search_tool_contract() -> ToolContract:
    return ToolContract(
        tool_name="web_search",
        version="1.0",
        consumer="research-agent",
        provider="search-service",
        input_fields=[
            FieldContract("query", "string", required=True),
            FieldContract("max_results", "integer", required=False, min_value=1, max_value=100),
            FieldContract("language", "string", required=False),
        ],
        output_fields=[
            FieldContract("results", "array", required=True),
            FieldContract("total_count", "integer", required=True, min_value=0),
            FieldContract("query_time_ms", "number", required=False),
        ],
        invariants=[
            "results array length <= max_results input",
            "each result has 'title' and 'url' fields",
        ],
    )
```

---

## Solution 3: Provider-Side Contract Test Runner

The tool provider runs the agent's contract against its own implementation to prove it satisfies the consumer's expectations.

```python
import asyncio
from typing import Callable, Awaitable

@dataclass
class ProviderTestCase:
    description: str
    input: dict
    expected_output_fields: list[str]  # Fields that must be present

@dataclass
class ProviderContractTestResult:
    tool_name: str
    consumer: str
    total_tests: int
    passed: int
    failed: int
    violations: list[ContractViolation]
    passed_overall: bool

class ProviderContractTestRunner:
    """
    Run on the provider (tool implementation) side.
    The provider calls its own implementation and verifies the response
    satisfies the consumer's contract expectations.
    """

    def __init__(
        self,
        contract: ToolContract,
        tool_impl: Callable[..., Awaitable[dict]],
    ):
        self.contract = contract
        self.tool = tool_impl
        self.verifier = ConsumerContractVerifier(contract)

    def _generate_test_cases(self) -> list[ProviderTestCase]:
        """Generate test cases from the contract input fields."""
        cases = []

        # Happy path: provide all required fields
        happy_input = {}
        for f in self.contract.input_fields:
            if f.required:
                if f.type == "string":
                    happy_input[f.name] = f.enum_values[0] if f.enum_values else "test_value"
                elif f.type == "integer":
                    v = int(f.min_value or 1)
                    happy_input[f.name] = v
                elif f.type == "boolean":
                    happy_input[f.name] = True
                elif f.type == "array":
                    happy_input[f.name] = []
                else:
                    happy_input[f.name] = {}
        cases.append(ProviderTestCase(
            description="Happy path with all required fields",
            input=happy_input,
            expected_output_fields=[f.name for f in self.contract.output_fields if f.required],
        ))

        return cases

    async def run(self, extra_cases: list[ProviderTestCase] | None = None) -> ProviderContractTestResult:
        test_cases = self._generate_test_cases() + (extra_cases or [])
        all_violations = []
        passed = 0

        for case in test_cases:
            # Validate input matches contract
            input_result = self.verifier.verify_input(case.input)
            if not input_result.passed:
                all_violations.extend(input_result.violations)
                continue

            # Call the tool implementation
            try:
                output = await self.tool(**case.input)
            except Exception as exc:
                all_violations.append(ContractViolation(
                    field="__execution__",
                    expected="no exception",
                    actual=str(exc),
                    severity="breaking",
                ))
                continue

            # Validate output matches contract
            output_result = self.verifier.verify_output(output)
            if output_result.passed:
                passed += 1
            else:
                all_violations.extend(output_result.violations)

        return ProviderContractTestResult(
            tool_name=self.contract.tool_name,
            consumer=self.contract.consumer,
            total_tests=len(test_cases),
            passed=passed,
            failed=len(test_cases) - passed,
            violations=all_violations,
            passed_overall=len(all_violations) == 0,
        )
```

---

## Solution 4: Contract Registry and Version Compatibility Checker

Centrally manage all contracts and detect breaking changes when new versions are published.

```python
from dataclasses import dataclass
import json, os

@dataclass
class CompatibilityReport:
    tool_name: str
    old_version: str
    new_version: str
    breaking_changes: list[str]
    additions: list[str]
    removals: list[str]
    is_compatible: bool

class ContractRegistry:
    """
    Stores and versions tool contracts. Detects breaking changes between versions.
    Consumers publish contracts; providers verify against them.
    """

    def __init__(self, registry_dir: str = "./contracts"):
        self._dir = registry_dir
        os.makedirs(registry_dir, exist_ok=True)

    def _path(self, tool_name: str, consumer: str, version: str) -> str:
        return os.path.join(self._dir, f"{consumer}__{tool_name}__{version}.json")

    def publish(self, contract: ToolContract) -> str:
        path = self._path(contract.tool_name, contract.consumer, contract.version)
        contract.save(path)
        return path

    def get_latest(self, tool_name: str, consumer: str) -> Optional[dict]:
        """Return the most recently published contract for a (tool, consumer) pair."""
        matches = [
            f for f in os.listdir(self._dir)
            if f.startswith(f"{consumer}__{tool_name}__") and f.endswith(".json")
        ]
        if not matches:
            return None
        latest = sorted(matches)[-1]
        with open(os.path.join(self._dir, latest)) as f:
            return json.load(f)

    def check_compatibility(self, old_schema: dict, new_schema: dict) -> CompatibilityReport:
        """
        Compare two output schemas and identify breaking changes.
        Breaking changes: removed required fields, changed field types, removed enum values.
        """
        old_props = old_schema.get("properties", {})
        new_props = new_schema.get("properties", {})
        old_required = set(old_schema.get("required", []))
        new_required = set(new_schema.get("required", []))

        breaking = []
        additions = []
        removals = []

        # Fields removed from required output
        for field in old_required:
            if field not in new_props:
                breaking.append(f"Required field '{field}' removed")
                removals.append(field)

        # Type changes
        for field in old_props:
            if field in new_props:
                old_type = old_props[field].get("type")
                new_type = new_props[field].get("type")
                if old_type != new_type:
                    breaking.append(f"Field '{field}' type changed: {old_type} -> {new_type}")

                # Removed enum values
                old_enum = set(old_props[field].get("enum", []))
                new_enum = set(new_props[field].get("enum", []))
                removed_enums = old_enum - new_enum
                if removed_enums:
                    breaking.append(f"Field '{field}' lost enum values: {removed_enums}")

        # New required fields (breaking for consumers that don't send them)
        for field in new_required - old_required:
            if field not in {f for f in old_props}:
                breaking.append(f"New required input field '{field}' added")

        # Non-breaking additions
        for field in new_props:
            if field not in old_props:
                additions.append(field)

        return CompatibilityReport(
            tool_name="",
            old_version="",
            new_version="",
            breaking_changes=breaking,
            additions=additions,
            removals=removals,
            is_compatible=len(breaking) == 0,
        )
```

---

## Solution 5: Intercepting Contract Validator (Middleware)

Wrap tool calls in production with contract validation middleware to catch violations in live traffic.

```python
import asyncio
import logging
from functools import wraps

logger = logging.getLogger(__name__)

class ContractValidationMiddleware:
    """
    Wraps tool calls with runtime contract validation.
    In strict mode, raises on violations. In soft mode, logs and continues.
    """

    def __init__(
        self,
        contract: ToolContract,
        strict: bool = False,
        alert_fn: Optional[Callable] = None,
    ):
        self.verifier = ConsumerContractVerifier(contract)
        self.strict = strict
        self.alert_fn = alert_fn
        self._violation_counts: dict[str, int] = {}

    def wrap(self, tool_fn: Callable) -> Callable:
        """Decorator that validates inputs and outputs against the contract."""
        verifier = self.verifier
        strict = self.strict
        alert_fn = self.alert_fn
        violation_counts = self._violation_counts

        @wraps(tool_fn)
        async def validated_tool(**kwargs):
            # Validate input
            input_result = verifier.verify_input(kwargs)
            if not input_result.passed:
                for v in input_result.violations:
                    key = f"input:{v.field}"
                    violation_counts[key] = violation_counts.get(key, 0) + 1
                    logger.warning(
                        "Contract input violation for %s.%s: expected %s got %s",
                        verifier.contract.tool_name, v.field, v.expected, v.actual,
                    )
                if strict and any(v.severity == "breaking" for v in input_result.violations):
                    raise ValueError(f"Contract input violation: {input_result.violations[0]}")

            # Execute tool
            result = await tool_fn(**kwargs)

            # Validate output
            output_result = verifier.verify_output(result)
            if not output_result.passed:
                for v in output_result.violations:
                    key = f"output:{v.field}"
                    violation_counts[key] = violation_counts.get(key, 0) + 1
                    logger.warning(
                        "Contract output violation for %s.%s: expected %s got %s",
                        verifier.contract.tool_name, v.field, v.expected, v.actual,
                    )
                if alert_fn:
                    asyncio.create_task(alert_fn(output_result.violations))
                if strict and any(v.severity == "breaking" for v in output_result.violations):
                    raise ValueError(f"Contract output violation: {output_result.violations[0]}")

            return result

        return validated_tool

    def violation_report(self) -> dict:
        return dict(sorted(self._violation_counts.items(), key=lambda x: -x[1]))
```

---

## Solution 6: Contract CI Gate

Block deployments when new tool versions break existing consumer contracts.

```python
import asyncio
import sys

class ContractCIGate:
    """
    CI/CD integration: fail the build if any registered consumer contract is violated.
    Runs as part of the provider's (tool's) CI pipeline.
    """

    def __init__(self, registry: ContractRegistry):
        self.registry = registry

    async def check_tool_release(
        self,
        tool_name: str,
        new_output_schema: dict,
        tool_impl: Optional[Callable] = None,
    ) -> bool:
        """
        Check all registered consumer contracts against a new tool version.
        Returns True if all contracts pass (safe to deploy).
        """
        all_passed = True
        print(f"Checking contracts for {tool_name} release...")

        # Find all consumers of this tool
        import glob
        pattern = os.path.join(self.registry._dir, f"*__{tool_name}__*.json")
        contract_files = glob.glob(pattern)

        if not contract_files:
            print(f"  No contracts registered for {tool_name}")
            return True

        for path in contract_files:
            with open(path) as f:
                contract_data = json.load(f)

            consumer = contract_data.get("consumer", "unknown")
            print(f"  Checking contract from consumer: {consumer}")

            # Schema compatibility check
            old_output = contract_data.get("output_schema", {})
            report = self.registry.check_compatibility(old_output, new_output_schema)

            if not report.is_compatible:
                print(f"  BREAKING CHANGES detected for consumer {consumer}:")
                for change in report.breaking_changes:
                    print(f"    - {change}")
                all_passed = False
            else:
                print(f"  Compatible (additions: {report.additions})")

            # Run provider tests if implementation provided
            if tool_impl:
                # Build contract object from stored data
                contract = create_search_tool_contract()  # Placeholder
                runner = ProviderContractTestRunner(contract, tool_impl)
                result = await runner.run()
                if not result.passed_overall:
                    print(f"  Provider tests FAILED: {result.failed}/{result.total_tests} tests failed")
                    all_passed = False
                else:
                    print(f"  Provider tests passed: {result.passed}/{result.total_tests}")

        print(f"\nOverall: {'PASS' if all_passed else 'FAIL'}")
        return all_passed

    async def run_and_exit(self, tool_name: str, new_output_schema: dict) -> None:
        """Use in CI scripts — exits with code 1 on failure."""
        passed = await self.check_tool_release(tool_name, new_output_schema)
        sys.exit(0 if passed else 1)
```

---

## Comparison

| Solution | Who Runs It | When Run | Blocking | Best For |
|---|---|---|---|---|
| Contract Definition | Consumer (agent) | Design time | No | Documenting expectations |
| Consumer Verifier | Consumer (agent) | Integration tests | Optional | Catching provider changes early |
| Provider Test Runner | Provider (tool) | Provider CI | Yes | Provider proving compliance |
| Contract Registry | Both | CI/CD | Optional | Central contract management |
| Validation Middleware | Consumer (agent) | Production | Configurable | Runtime drift detection |
| CI Gate | Provider (tool) | Pre-deploy | Yes | Blocking breaking releases |

**Define contracts for every tool the agent depends on** — even a minimal schema covering required output fields provides enormous value. **Deploy the validation middleware** in soft mode (log-only) in production to detect contract violations in live traffic without causing outages. **Add the provider CI gate** to the tool team's pipeline so they get immediate feedback when a proposed change would break the agent. **Use the contract registry** as the shared source of truth that both teams reference. Run **provider tests in CI** before every tool release as a hard gate on breaking changes.
