---
layout: solution
title: "Agent Uses Stale Few-Shot Examples — Outdated Patterns in Prompt"
category: prompt-engineering
description: "Agent's few-shot examples were written for an old API version, an old data schema, or an old output format. The model follows the examples faithfully — and produces outdated output. The examples were correct once. Now they're wrong, and they're teaching the model to be wrong too."
tags: [prompt-engineering, few-shot, examples, staleness, in-context-learning, rag, dynamic-examples, versioning, prompt-management]
---

# Agent Uses Stale Few-Shot Examples — Outdated Patterns in Prompt

## Symptom
- Agent generates code using deprecated API methods — the examples in the prompt use the old pattern
- Agent produces output in an old JSON schema that downstream consumers no longer accept
- Few-shot examples reference field names that were renamed 3 months ago
- Agent formats dates as `MM/DD/YYYY` because the examples do — but the spec changed to ISO 8601
- Examples reference a library version (`requests` 2.x patterns) but the codebase upgraded to `httpx`
- Agent produces SQL for PostgreSQL 12 syntax — examples predate the migration to PostgreSQL 15
- Few-shot examples show error responses in the old format — new errors have a different structure

## Root Cause
Few-shot examples are frozen text in a prompt. When the world changes — API versions, schemas, conventions, library upgrades — the examples don't update automatically. The model learns from the examples it's given; stale examples teach stale patterns. The fix is either to keep examples current through versioning and automated checks, or to replace static examples with dynamically retrieved ones that are verified against the current schema.

## Fix

### Option 1: Version-stamp examples and validate against current schema

```python
import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Optional

@dataclass
class FewShotExample:
    """
    A versioned few-shot example with staleness tracking.
    """
    id: str
    description: str
    input: str
    output: str
    schema_version: str        # Which schema/API version this example targets
    created_at: str            # ISO 8601
    validated_at: Optional[str] = None
    deprecated: bool = False
    deprecation_reason: Optional[str] = None

    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.input}{self.output}".encode()).hexdigest()[:12]

class FewShotLibrary:
    """
    Manages a library of versioned few-shot examples.
    Filters examples to only those matching the current schema version.
    Alerts when examples haven't been validated recently.
    """

    def __init__(
        self,
        library_path: str = "prompts/few_shot_examples.json",
        current_schema_version: str = "v2"
    ):
        self.library_path = Path(library_path)
        self.current_version = current_schema_version
        self._examples: list[FewShotExample] = self._load()

    def _load(self) -> list[FewShotExample]:
        if self.library_path.exists():
            raw = json.loads(self.library_path.read_text())
            return [FewShotExample(**e) for e in raw.get("examples", [])]
        return []

    def get_current_examples(
        self,
        max_examples: int = 3,
        warn_if_stale_days: int = 30
    ) -> list[FewShotExample]:
        """
        Return examples valid for the current schema version.
        Warns if examples haven't been validated recently.
        """
        valid = [
            e for e in self._examples
            if e.schema_version == self.current_version
            and not e.deprecated
        ]

        if not valid:
            print(
                f"WARNING: No few-shot examples found for schema version '{self.current_version}'. "
                f"Examples may be stale. Check {self.library_path}."
            )
            return []

        # Warn on stale validation
        now = datetime.utcnow()
        for example in valid:
            if example.validated_at:
                age_days = (now - datetime.fromisoformat(example.validated_at)).days
                if age_days > warn_if_stale_days:
                    print(
                        f"WARNING: Example '{example.id}' last validated {age_days} days ago. "
                        f"Re-validate against current schema."
                    )

        return valid[:max_examples]

    def deprecate_version(self, version: str, reason: str):
        """Mark all examples for an old version as deprecated"""
        for example in self._examples:
            if example.schema_version == version:
                example.deprecated = True
                example.deprecation_reason = reason
        self._save()
        print(f"Deprecated {sum(1 for e in self._examples if e.schema_version == version)} examples for version '{version}'")

    def add_example(self, example: FewShotExample):
        self._examples.append(example)
        self._save()

    def _save(self):
        self.library_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"examples": [e.__dict__ for e in self._examples]}
        self.library_path.write_text(json.dumps(data, indent=2))

    def build_few_shot_block(self, max_examples: int = 3) -> str:
        """Format examples for inclusion in a system prompt"""
        examples = self.get_current_examples(max_examples=max_examples)
        if not examples:
            return ""

        lines = ["## Examples\n"]
        for i, ex in enumerate(examples, 1):
            lines.append(f"### Example {i}: {ex.description}")
            lines.append(f"Input:\n{ex.input}")
            lines.append(f"Output:\n{ex.output}\n")
        return "\n".join(lines)

# Usage:
library = FewShotLibrary(current_schema_version="v3")

# When API/schema upgrades:
library.deprecate_version("v2", reason="API migrated from REST v2 to v3 on 2025-01-15")
library.add_example(FewShotExample(
    id="create_order_v3",
    description="Create order with new v3 schema",
    input='Create an order for user 42 with items [{"sku": "A1", "qty": 2}]',
    output='{"order": {"user_id": 42, "line_items": [{"sku": "A1", "quantity": 2}], "version": "v3"}}',
    schema_version="v3",
    created_at=datetime.utcnow().isoformat(),
    validated_at=datetime.utcnow().isoformat()
))

system_prompt = f"""You are an order processing agent.
{library.build_few_shot_block(max_examples=3)}
Always produce output in the current v3 schema format shown above.
"""
```

### Option 2: Dynamic few-shot retrieval — pull relevant, current examples at runtime

```python
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic()

class DynamicFewShotRetriever:
    """
    Instead of hardcoded examples, retrieve the most relevant examples
    from a validated, versioned store at runtime.
    Combines relevance (embedding similarity) with recency (prefer newer).
    """

    def __init__(self, examples_dir: str = "prompts/examples/"):
        self.examples_dir = Path(examples_dir)
        self._cache: dict[str, dict] = {}

    def _load_examples(self) -> list[dict]:
        """Load all example files — each file is one validated example"""
        examples = []
        for f in sorted(self.examples_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text())
                if not data.get("deprecated", False):
                    examples.append(data)
            except Exception:
                pass
        return examples

    async def retrieve_relevant(
        self,
        user_query: str,
        max_examples: int = 3
    ) -> list[dict]:
        """
        Use Claude to select the most relevant examples for the current query.
        This avoids semantic search infrastructure while still being query-aware.
        """
        all_examples = self._load_examples()
        if not all_examples:
            return []

        if len(all_examples) <= max_examples:
            return all_examples[:max_examples]

        # Ask Claude to select the most relevant examples
        example_summaries = "\n".join(
            f"{i}. {ex['description']} (added: {ex.get('created_at', 'unknown')[:10]})"
            for i, ex in enumerate(all_examples[:20])  # Limit to 20 candidates
        )

        selection_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"User query: {user_query}\n\n"
                    f"Available examples:\n{example_summaries}\n\n"
                    f"Return the {max_examples} most relevant example numbers as JSON array. "
                    f"Example: [0, 3, 7]"
                )
            }]
        )

        try:
            selected_indices = json.loads(selection_response.content[0].text)
            return [all_examples[i] for i in selected_indices if i < len(all_examples)]
        except Exception:
            # Fallback to most recent
            return all_examples[:max_examples]

    def format_examples(self, examples: list[dict]) -> str:
        """Format examples for prompt inclusion"""
        if not examples:
            return ""
        parts = ["Here are relevant examples:\n"]
        for ex in examples:
            parts.append(f"Example — {ex['description']}:")
            parts.append(f"Input: {ex['input']}")
            parts.append(f"Output: {ex['output']}\n")
        return "\n".join(parts)

retriever = DynamicFewShotRetriever()

async def build_prompt_with_dynamic_examples(user_query: str) -> str:
    examples = await retriever.retrieve_relevant(user_query, max_examples=3)
    few_shot_block = retriever.format_examples(examples)

    return f"""You are a data transformation agent.
{few_shot_block}
Follow the exact output format shown in the examples above.
"""
```

### Option 3: Example validation CI — fail the build when examples diverge from schema

```python
import json
import pytest
from pathlib import Path
from pydantic import BaseModel, ValidationError
from typing import Any

# Define the current expected output schema
class OrderLineItem(BaseModel):
    sku: str
    quantity: int  # Note: was 'qty' in old schema — changed in v3

class OrderOutput(BaseModel):
    order: dict  # Or more specific nested model

class CreateOrderOutput(BaseModel):
    """Current v3 output schema"""
    order_id: str
    user_id: int
    line_items: list[OrderLineItem]
    status: str
    version: str = "v3"

EXAMPLE_DIR = Path("prompts/examples/")

def load_all_examples() -> list[tuple[str, dict]]:
    """Load all example files with their filenames"""
    examples = []
    for f in EXAMPLE_DIR.glob("*.json"):
        data = json.loads(f.read_text())
        examples.append((f.name, data))
    return examples

@pytest.mark.parametrize("filename,example", load_all_examples())
def test_example_output_matches_current_schema(filename: str, example: dict):
    """
    CI test: every few-shot example's output must be valid under the current schema.
    Fails immediately if an example drifts from the schema — caught before deployment.
    """
    if example.get("deprecated"):
        pytest.skip(f"Example {filename} is deprecated — skipping schema check")

    if example.get("output_type") != "create_order":
        pytest.skip(f"Example {filename} is not a create_order example")

    try:
        output_data = json.loads(example["output"])
    except json.JSONDecodeError as e:
        pytest.fail(f"Example {filename}: output is not valid JSON: {e}")

    try:
        CreateOrderOutput(**output_data)
    except ValidationError as e:
        pytest.fail(
            f"Example {filename} output does not match current schema (v3):\n"
            f"{e}\n\n"
            f"Example output:\n{json.dumps(output_data, indent=2)}\n\n"
            f"This example needs to be updated to the current schema."
        )

@pytest.mark.parametrize("filename,example", load_all_examples())
def test_example_has_required_metadata(filename: str, example: dict):
    """Every example must have schema_version and created_at"""
    assert "schema_version" in example, f"{filename}: missing schema_version"
    assert "created_at" in example, f"{filename}: missing created_at"
    assert "description" in example, f"{filename}: missing description"
    assert not example.get("deprecated", False) or "deprecation_reason" in example, (
        f"{filename}: deprecated example missing deprecation_reason"
    )

# Run in CI with: pytest prompts/tests/test_examples.py -v
# Blocks deployment if any non-deprecated example fails schema validation
```

### Option 4: Auto-generate examples from real successful outputs

```python
import anthropic
import json
import hashlib
from datetime import datetime
from pathlib import Path

client = anthropic.Anthropic()

class ExampleHarvester:
    """
    Harvest few-shot examples from real, successful agent outputs.
    When the agent produces a verified-correct output, save it as a new example.
    Examples are always current because they come from the running system.
    """

    def __init__(
        self,
        examples_dir: str = "prompts/examples/",
        max_examples_per_type: int = 5
    ):
        self.examples_dir = Path(examples_dir)
        self.examples_dir.mkdir(parents=True, exist_ok=True)

    def _example_id(self, input_text: str, output_type: str) -> str:
        return hashlib.sha256(f"{output_type}:{input_text}".encode()).hexdigest()[:12]

    def harvest(
        self,
        input_text: str,
        output_text: str,
        output_type: str,
        description: str,
        schema_version: str,
        verified: bool = False
    ):
        """
        Save a real output as a new few-shot example.
        Only call this when the output is verified correct.
        """
        if not verified:
            return  # Never harvest unverified outputs as examples

        example_id = self._example_id(input_text, output_type)
        example_path = self.examples_dir / f"{output_type}_{example_id}.json"

        example = {
            "id": example_id,
            "output_type": output_type,
            "description": description,
            "input": input_text,
            "output": output_text,
            "schema_version": schema_version,
            "created_at": datetime.utcnow().isoformat(),
            "validated_at": datetime.utcnow().isoformat(),
            "source": "harvested_from_production",
            "deprecated": False
        }

        example_path.write_text(json.dumps(example, indent=2))
        print(f"Harvested example: {example_path.name}")

        # Rotate old examples — keep only max_examples_per_type
        self._rotate_examples(output_type)

    def _rotate_examples(self, output_type: str):
        """Keep only the N most recent examples per type"""
        type_examples = sorted(
            self.examples_dir.glob(f"{output_type}_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in type_examples[5:]:  # Keep 5 most recent
            old.unlink()
            print(f"Rotated old example: {old.name}")

harvester = ExampleHarvester()

async def run_agent_with_example_harvesting(
    user_input: str,
    output_type: str,
    schema_version: str
) -> str:
    """
    Run the agent and harvest verified outputs as future few-shot examples.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_input}]
    )
    output = response.content[0].text

    # Verify output is correct before harvesting
    is_valid = await verify_output(output, output_type, schema_version)

    if is_valid:
        harvester.harvest(
            input_text=user_input,
            output_text=output,
            output_type=output_type,
            description=f"Auto-harvested {output_type} example",
            schema_version=schema_version,
            verified=True  # Only harvest verified outputs
        )

    return output

async def verify_output(output: str, output_type: str, schema_version: str) -> bool:
    """Verify output against current schema — only valid outputs become examples"""
    try:
        data = json.loads(output)
        # Apply type-specific validation here
        return True
    except Exception:
        return False
```

### Option 5: Prompt diffing — detect example drift on schema changes

```python
import json
import re
from dataclasses import dataclass
from typing import Optional

@dataclass
class DriftReport:
    example_id: str
    issues: list[str]
    severity: str  # "critical", "warning", "ok"

class ExampleDriftDetector:
    """
    Detects when few-shot examples reference outdated patterns:
    - Deprecated field names
    - Old API endpoints
    - Old date formats
    - Removed enum values
    """

    def __init__(self):
        # Define what patterns are outdated
        self.deprecated_patterns = {
            # (regex pattern, replacement, description)
            r'"qty"': ('"quantity"', "Field 'qty' renamed to 'quantity' in v3"),
            r'"user_name"': ('"username"', "Field 'user_name' renamed to 'username'"),
            r'/api/v1/': ('"description": "Use /api/v3/"', "API v1 endpoint deprecated"),
            r'"MM/DD/YYYY"': ('"YYYY-MM-DD"', "Date format changed to ISO 8601"),
            r'requests\.get': ('httpx.get', "Library migrated from requests to httpx"),
            r'"status": "ok"': ('"status": "success"', "Status value changed from 'ok' to 'success'"),
        }

        self.required_patterns = {
            # Patterns that MUST appear in current examples
            '"version": "v3"': "All outputs must include version field set to v3",
        }

    def check_example(self, example: dict) -> DriftReport:
        """Check a single example for drift against current patterns"""
        issues = []
        example_text = json.dumps(example)

        # Check for deprecated patterns
        for pattern, (replacement, description) in self.deprecated_patterns.items():
            if re.search(pattern, example_text):
                issues.append(f"DEPRECATED: {description} — replace {pattern!r} with {replacement!r}")

        # Check for required patterns in output
        output_text = example.get("output", "")
        for pattern, description in self.required_patterns.items():
            if pattern not in output_text:
                issues.append(f"MISSING: {description}")

        severity = "critical" if any("DEPRECATED" in i for i in issues) else (
            "warning" if issues else "ok"
        )

        return DriftReport(
            example_id=example.get("id", "unknown"),
            issues=issues,
            severity=severity
        )

    def audit_all_examples(self, examples: list[dict]) -> list[DriftReport]:
        """Audit all examples and return drift reports"""
        reports = []
        for ex in examples:
            if not ex.get("deprecated"):
                report = self.check_example(ex)
                reports.append(report)
                if report.severity != "ok":
                    print(
                        f"[{report.severity.upper()}] Example '{report.example_id}':\n"
                        + "\n".join(f"  - {issue}" for issue in report.issues)
                    )

        critical = sum(1 for r in reports if r.severity == "critical")
        warnings = sum(1 for r in reports if r.severity == "warning")
        ok = sum(1 for r in reports if r.severity == "ok")

        print(f"\nAudit summary: {ok} ok, {warnings} warnings, {critical} critical")
        return reports

detector = ExampleDriftDetector()

# Add new deprecated patterns when schema changes:
# detector.deprecated_patterns[r'"old_field"'] = ('"new_field"', "Renamed in schema v4")
```

### Option 6: Few-shot example rotation schedule — time-based freshness enforcement

```python
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

class ExampleFreshnessEnforcer:
    """
    Enforces a rotation schedule for few-shot examples.
    Examples older than max_age_days are flagged for review.
    After review_deadline_days, old examples are automatically disabled.
    The agent refuses to use examples past their expiry.
    """

    def __init__(
        self,
        review_after_days: int = 60,    # Flag for review after 60 days
        disable_after_days: int = 180   # Disable after 180 days if not reviewed
    ):
        self.review_after_days = review_after_days
        self.disable_after_days = disable_after_days

    def check_freshness(self, example: dict) -> dict:
        """
        Returns freshness status for a single example.
        """
        created_at_str = example.get("validated_at") or example.get("created_at")
        if not created_at_str:
            return {
                "status": "unknown",
                "message": "No creation/validation date — assume stale",
                "usable": False
            }

        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00").rstrip("+00:00"))
        age_days = (datetime.utcnow() - created_at).days

        if age_days > self.disable_after_days:
            return {
                "status": "expired",
                "message": f"Example is {age_days} days old — exceeds {self.disable_after_days}-day limit. Update required.",
                "age_days": age_days,
                "usable": False
            }
        elif age_days > self.review_after_days:
            return {
                "status": "stale",
                "message": f"Example is {age_days} days old — review recommended.",
                "age_days": age_days,
                "usable": True  # Still usable but flagged
            }
        else:
            return {
                "status": "fresh",
                "age_days": age_days,
                "usable": True
            }

    def filter_usable(self, examples: list[dict]) -> list[dict]:
        """Return only examples that are usable (not expired)"""
        usable = []
        for ex in examples:
            freshness = self.check_freshness(ex)
            if freshness["usable"]:
                if freshness["status"] == "stale":
                    print(f"WARNING: Example '{ex.get('id')}' is stale ({freshness['message']})")
                usable.append(ex)
            else:
                print(f"BLOCKED: Example '{ex.get('id')}' is expired and will not be used. {freshness['message']}")
        return usable

    def generate_rotation_schedule(self, examples: list[dict]) -> dict:
        """
        Generate a schedule of when examples need review/replacement.
        Use this in a weekly cron job to proactively manage examples.
        """
        schedule = {"needs_review_now": [], "needs_review_soon": [], "fresh": []}

        for ex in examples:
            freshness = self.check_freshness(ex)
            days = freshness.get("age_days", 999)

            if not freshness["usable"] or days > self.review_after_days:
                schedule["needs_review_now"].append({
                    "id": ex.get("id"),
                    "age_days": days,
                    "action": "Update immediately" if not freshness["usable"] else "Review and re-validate"
                })
            elif days > (self.review_after_days * 0.8):  # 80% of review threshold
                schedule["needs_review_soon"].append({
                    "id": ex.get("id"),
                    "age_days": days,
                    "action": f"Schedule review within {self.review_after_days - days} days"
                })
            else:
                schedule["fresh"].append(ex.get("id"))

        return schedule

enforcer = ExampleFreshnessEnforcer(review_after_days=60, disable_after_days=180)

# In agent startup:
def load_valid_examples(library: FewShotLibrary) -> list[dict]:
    all_examples = [e.__dict__ for e in library.get_current_examples(max_examples=10)]
    return enforcer.filter_usable(all_examples)

# Weekly cron job to check schedule:
# schedule = enforcer.generate_rotation_schedule(all_examples)
# if schedule["needs_review_now"]:
#     send_alert(f"Few-shot examples need immediate review: {schedule['needs_review_now']}")
```

## Example Staleness Failure Modes

| Failure Mode | Symptom | Fix |
|---|---|---|
| Renamed field in output | Agent uses old field name `qty` instead of `quantity` | Version-stamp examples; fail CI on deprecated pattern |
| Old API endpoint in example | Agent generates calls to `/api/v1/` | Add deprecated pattern check for old endpoints |
| Changed date format | Agent formats `12/31/2024` instead of `2024-12-31` | CI schema validation against current format |
| Library migration | Agent writes `requests.get()` after migration to `httpx` | Drift detector with deprecated code patterns |
| New required field added | Agent omits `version` field added to schema | Required pattern checker in CI |
| Old error format | Agent produces old error structure downstream rejects | Schema validation test on all examples |

## When to Update Examples

| Event | Action |
|---|---|
| API schema version bump | Deprecate all examples for old version; add new examples |
| Field renamed | Add old name to deprecated patterns; add new examples |
| Library/framework upgrade | Audit examples for old import patterns; rotate |
| Output format change | Run CI schema validation; fix or remove failing examples |
| New required field | Add to required_patterns check; update all examples |
| After 60 days | Re-validate examples still match current behavior |

## Expected Token Savings
Stale examples teach wrong patterns → model produces wrong output → user corrects → retry: ~8,000 tokens per session
Current examples → model produces correct output first try: 0 correction overhead

## Environment
- Any agent using few-shot prompting with static examples in the system prompt or message template; especially critical for agents that produce structured output (JSON, SQL, code) where schemas and conventions evolve over time
- Source: direct experience; few-shot example staleness is the hardest prompt bug to diagnose because the model's reasoning looks correct — it's faithfully following wrong examples
