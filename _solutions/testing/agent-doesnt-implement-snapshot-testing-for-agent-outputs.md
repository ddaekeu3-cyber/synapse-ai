---
layout: solution
title: "Agent Doesn't Implement Snapshot Testing for Agent Outputs"
category: testing
description: "Snapshot testing captures approved agent outputs as baselines and alerts when future outputs deviate — enabling regression detection for prompt changes, model upgrades, and tool modifications without requiring manual re-evaluation every run."
tags: [testing, snapshot, regression, evaluation, quality-assurance, baselines]
---

## Problem

When you change a prompt, upgrade a model, or modify a tool, you want to know if agent outputs have changed in unexpected ways. Without snapshot baselines, every change requires manual review of all outputs. Snapshot testing automatically detects regressions by comparing current outputs against previously approved responses using exact match, semantic similarity, or structured field comparison.

## Solutions

### Option 1: Exact Snapshot Match with Approval Workflow

```python
import anthropic
import json
import os
import hashlib
from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

client = anthropic.Anthropic()

SNAPSHOT_DIR = Path("/tmp/agent_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)

@dataclass
class Snapshot:
    test_id: str
    prompt: str
    system: str
    model: str
    output: str
    output_hash: str
    approved: bool = False
    created_at: str = ""

def snapshot_path(test_id: str) -> Path:
    return SNAPSHOT_DIR / f"{test_id}.json"

def load_snapshot(test_id: str) -> Optional[Snapshot]:
    path = snapshot_path(test_id)
    if path.exists():
        data = json.loads(path.read_text())
        return Snapshot(**data)
    return None

def save_snapshot(snap: Snapshot):
    path = snapshot_path(snap.test_id)
    path.write_text(json.dumps(asdict(snap), indent=2))

def run_agent(prompt: str, system: str, model: str = "claude-haiku-4-5-20251001") -> str:
    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

def snapshot_test(
    test_id: str,
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    auto_approve_new: bool = False
) -> dict:
    """
    Run agent and compare to snapshot.
    Returns test result with pass/fail/new status.
    """
    import datetime
    current_output = run_agent(prompt, system, model)
    current_hash = hashlib.sha256(current_output.encode()).hexdigest()[:12]

    existing = load_snapshot(test_id)

    if existing is None:
        # New snapshot — save it
        snap = Snapshot(
            test_id=test_id,
            prompt=prompt,
            system=system,
            model=model,
            output=current_output,
            output_hash=current_hash,
            approved=auto_approve_new,
            created_at=datetime.datetime.utcnow().isoformat()
        )
        save_snapshot(snap)
        status = "new_approved" if auto_approve_new else "new_pending_approval"
        print(f"[SNAPSHOT] {test_id}: {status}")
        return {"status": status, "test_id": test_id, "output": current_output}

    if existing.output_hash == current_hash:
        print(f"[SNAPSHOT] {test_id}: PASS (exact match)")
        return {"status": "pass", "test_id": test_id, "output": current_output}

    # Mismatch detected
    print(f"[SNAPSHOT] {test_id}: FAIL (output changed)")
    print(f"  Previous: {existing.output[:100]}...")
    print(f"  Current:  {current_output[:100]}...")
    print(f"  Hash: {existing.output_hash} → {current_hash}")

    return {
        "status": "fail",
        "test_id": test_id,
        "previous_output": existing.output,
        "current_output": current_output,
        "previous_hash": existing.output_hash,
        "current_hash": current_hash
    }

def approve_snapshot(test_id: str, current_output: str, model: str, prompt: str, system: str):
    """Update snapshot with new approved output."""
    import datetime
    snap = Snapshot(
        test_id=test_id, prompt=prompt, system=system, model=model,
        output=current_output,
        output_hash=hashlib.sha256(current_output.encode()).hexdigest()[:12],
        approved=True,
        created_at=datetime.datetime.utcnow().isoformat()
    )
    save_snapshot(snap)
    print(f"[SNAPSHOT] {test_id}: approved and updated")

# Usage
SYSTEM = "You are a concise technical assistant."

results = [
    snapshot_test("greet_user", "Say hello in exactly one sentence.", SYSTEM, auto_approve_new=True),
    snapshot_test("list_python_types", "List Python's 4 primitive types.", SYSTEM, auto_approve_new=True),
    snapshot_test("explain_async", "What is async/await in one sentence?", SYSTEM, auto_approve_new=True),
]

for r in results:
    print(f"  {r['test_id']}: {r['status']}")

# Expected Token Savings: Regressions caught before expensive manual review cycles
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/agent_snapshots/
```

### Option 2: Semantic Similarity Snapshot (Fuzzy Matching)

```python
import anthropic
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

client = anthropic.Anthropic()

SEMANTIC_SNAPSHOT_DIR = Path("/tmp/semantic_snapshots")
SEMANTIC_SNAPSHOT_DIR.mkdir(exist_ok=True)

@dataclass
class SemanticSnapshot:
    test_id: str
    prompt: str
    reference_output: str
    key_phrases: list[str]   # Must appear in future outputs
    forbidden_phrases: list[str]  # Must NOT appear in future outputs
    min_similarity: float = 0.7

def extract_key_phrases(text: str, max_phrases: int = 8) -> list[str]:
    """Extract noun phrases and technical terms as required phrases."""
    # Simple extraction: multi-word capitalized terms and quoted content
    phrases = []
    # Capture quoted strings
    phrases.extend(re.findall(r'"([^"]{3,30})"', text))
    # Capture code identifiers
    phrases.extend(re.findall(r'`([^`]{2,20})`', text))
    # Capture capitalized multi-word terms
    phrases.extend(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', text))
    return list(set(phrases))[:max_phrases]

def jaccard_similarity(a: str, b: str) -> float:
    """Token-level Jaccard similarity between two texts."""
    tokens_a = set(re.findall(r'\w+', a.lower()))
    tokens_b = set(re.findall(r'\w+', b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

def check_key_phrases(output: str, key_phrases: list[str]) -> list[str]:
    """Return list of key phrases missing from output."""
    output_lower = output.lower()
    return [p for p in key_phrases if p.lower() not in output_lower]

def save_semantic_snapshot(snap: SemanticSnapshot):
    path = SEMANTIC_SNAPSHOT_DIR / f"{snap.test_id}.json"
    import dataclasses
    path.write_text(json.dumps(dataclasses.asdict(snap), indent=2))

def load_semantic_snapshot(test_id: str) -> Optional[SemanticSnapshot]:
    path = SEMANTIC_SNAPSHOT_DIR / f"{test_id}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return SemanticSnapshot(**data)
    return None

def create_semantic_snapshot(
    test_id: str,
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    forbidden_phrases: list[str] = None
):
    response = client.messages.create(
        model=model, max_tokens=300, system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text
    key_phrases = extract_key_phrases(output)

    snap = SemanticSnapshot(
        test_id=test_id, prompt=prompt, reference_output=output,
        key_phrases=key_phrases, forbidden_phrases=forbidden_phrases or []
    )
    save_semantic_snapshot(snap)
    print(f"[SemanticSnapshot] {test_id}: created with {len(key_phrases)} key phrases")
    return snap

def run_semantic_snapshot_test(
    test_id: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    snap = load_semantic_snapshot(test_id)
    if not snap:
        return {"status": "no_snapshot", "test_id": test_id}

    response = client.messages.create(
        model=model, max_tokens=300, system=system,
        messages=[{"role": "user", "content": snap.prompt}]
    )
    current = response.content[0].text

    similarity = jaccard_similarity(snap.reference_output, current)
    missing_phrases = check_key_phrases(current, snap.key_phrases)
    forbidden_found = [p for p in snap.forbidden_phrases if p.lower() in current.lower()]

    passed = (
        similarity >= snap.min_similarity
        and len(missing_phrases) == 0
        and len(forbidden_found) == 0
    )

    status = "pass" if passed else "fail"
    print(f"[SemanticSnapshot] {test_id}: {status}")
    print(f"  Similarity: {similarity:.0%} (min: {snap.min_similarity:.0%})")
    if missing_phrases:
        print(f"  Missing phrases: {missing_phrases}")
    if forbidden_found:
        print(f"  Forbidden phrases found: {forbidden_found}")

    return {
        "status": status, "test_id": test_id,
        "similarity": similarity, "missing_phrases": missing_phrases,
        "forbidden_found": forbidden_found, "current_output": current
    }

# Usage: create snapshots, then run tests
SYSTEM = "You are a concise technical writer."

# First run: create baselines
create_semantic_snapshot("async_explanation", "Explain async/await in Python in 2 sentences.", SYSTEM,
    forbidden_phrases=["I don't know", "I cannot"])
create_semantic_snapshot("list_benefits", "List 3 benefits of type hints in Python.", SYSTEM)

# Subsequent runs: compare against baselines
for test_id in ["async_explanation", "list_benefits"]:
    result = run_semantic_snapshot_test(test_id, SYSTEM)
    print(f"  → {result['status']}\n")

# Expected Token Savings: Semantic matching catches meaningful regressions, ignores trivial wording changes
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/semantic_snapshots/
```

### Option 3: Structured Field Snapshot (Schema-Validated)

```python
import anthropic
import json
import re
from dataclasses import dataclass
from typing import Any
from pathlib import Path

client = anthropic.Anthropic()

STRUCT_SNAPSHOT_DIR = Path("/tmp/struct_snapshots")
STRUCT_SNAPSHOT_DIR.mkdir(exist_ok=True)

@dataclass
class FieldAssertion:
    field_path: str          # e.g., "items[0].name"
    assertion_type: str      # "exists" | "equals" | "contains" | "type" | "count"
    expected_value: Any = None

def extract_json_from_response(text: str) -> dict:
    """Extract JSON object from model response."""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}

def get_nested_value(data: dict, path: str) -> Any:
    """Navigate nested dict/list with dot+bracket notation."""
    parts = re.split(r'\.|\[(\d+)\]', path)
    current = data
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            try:
                current = current[int(part)]
            except (IndexError, TypeError, KeyError):
                return None
        else:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
    return current

def check_assertion(data: dict, assertion: FieldAssertion) -> tuple[bool, str]:
    value = get_nested_value(data, assertion.field_path)

    if assertion.assertion_type == "exists":
        passed = value is not None
        return passed, f"Field '{assertion.field_path}' {'exists' if passed else 'missing'}"

    if assertion.assertion_type == "equals":
        passed = value == assertion.expected_value
        return passed, f"'{assertion.field_path}': expected {assertion.expected_value!r}, got {value!r}"

    if assertion.assertion_type == "contains":
        passed = assertion.expected_value in str(value or "")
        return passed, f"'{assertion.field_path}' should contain {assertion.expected_value!r}"

    if assertion.assertion_type == "type":
        type_map = {"str": str, "int": int, "float": float, "list": list, "dict": dict, "bool": bool}
        expected_type = type_map.get(assertion.expected_value, str)
        passed = isinstance(value, expected_type)
        return passed, f"'{assertion.field_path}' type: expected {assertion.expected_value}, got {type(value).__name__}"

    if assertion.assertion_type == "count":
        passed = isinstance(value, list) and len(value) == assertion.expected_value
        return passed, f"'{assertion.field_path}' count: expected {assertion.expected_value}, got {len(value) if isinstance(value, list) else 'N/A'}"

    return False, f"Unknown assertion type: {assertion.assertion_type}"

def run_structured_snapshot_test(
    test_id: str,
    prompt: str,
    assertions: list[FieldAssertion],
    system: str = "",
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    """Run agent and validate output structure against field assertions."""
    response = client.messages.create(
        model=model, max_tokens=400, system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text
    parsed = extract_json_from_response(output)

    results = []
    all_pass = True

    for assertion in assertions:
        passed, message = check_assertion(parsed, assertion)
        results.append({"assertion": assertion.field_path, "passed": passed, "message": message})
        if not passed:
            all_pass = False

    status = "pass" if all_pass else "fail"
    print(f"[StructSnapshot] {test_id}: {status} ({sum(r['passed'] for r in results)}/{len(results)} assertions)")
    for r in results:
        icon = "✓" if r["passed"] else "✗"
        print(f"  {icon} {r['message']}")

    return {"status": status, "test_id": test_id, "assertions": results, "raw_output": output}

# Usage: test JSON-structured agent output
SYSTEM = """You are a data extractor. Always respond with valid JSON."""

# Test 1: Product list extraction
result1 = run_structured_snapshot_test(
    test_id="product_extraction",
    prompt="""Extract products from: "We sell the ProX laptop for $999, the MiniTab tablet for $499, and the QuickPhone for $299."
Return JSON: {"products": [{"name": str, "price": number}]}""",
    assertions=[
        FieldAssertion("products", "exists"),
        FieldAssertion("products", "count", 3),
        FieldAssertion("products[0].name", "exists"),
        FieldAssertion("products[0].price", "type", "int"),
    ],
    system=SYSTEM
)

# Test 2: Sentiment analysis structure
result2 = run_structured_snapshot_test(
    test_id="sentiment_analysis",
    prompt="""Analyze: "The product is great but shipping was slow." Return JSON: {"sentiment": str, "score": number, "aspects": [str]}""",
    assertions=[
        FieldAssertion("sentiment", "exists"),
        FieldAssertion("score", "type", "float"),
        FieldAssertion("aspects", "exists"),
    ],
    system=SYSTEM
)

# Expected Token Savings: Schema validation catches structural regressions without manual inspection
# Environment: ANTHROPIC_API_KEY required, no file writes
```

### Option 4: Multi-Turn Conversation Snapshot

```python
import anthropic
import json
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

client = anthropic.Anthropic()

CONV_SNAPSHOT_DIR = Path("/tmp/conv_snapshots")
CONV_SNAPSHOT_DIR.mkdir(exist_ok=True)

@dataclass
class TurnSnapshot:
    turn: int
    user_message: str
    expected_output_hash: str
    expected_output_preview: str  # First 100 chars for human review
    key_assertions: list[str]    # Substrings that must appear

@dataclass
class ConversationSnapshot:
    test_id: str
    system: str
    turns: list[TurnSnapshot] = field(default_factory=list)

def save_conv_snapshot(snap: ConversationSnapshot):
    path = CONV_SNAPSHOT_DIR / f"{snap.test_id}.json"
    path.write_text(json.dumps(asdict(snap), indent=2))

def load_conv_snapshot(test_id: str) -> Optional[ConversationSnapshot]:
    path = CONV_SNAPSHOT_DIR / f"{test_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    snap = ConversationSnapshot(test_id=data["test_id"], system=data["system"])
    snap.turns = [TurnSnapshot(**t) for t in data["turns"]]
    return snap

def record_conversation_snapshot(
    test_id: str,
    conversation: list[str],   # Alternating user messages
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    key_assertions_per_turn: list[list[str]] = None
) -> ConversationSnapshot:
    """Record a full conversation as a snapshot baseline."""
    snap = ConversationSnapshot(test_id=test_id, system=system)
    messages = []
    assertions = key_assertions_per_turn or [[] for _ in conversation]

    for i, user_msg in enumerate(conversation):
        messages.append({"role": "user", "content": user_msg})
        response = client.messages.create(
            model=model, max_tokens=200, system=system, messages=messages
        )
        output = response.content[0].text
        messages.append({"role": "assistant", "content": output})

        snap.turns.append(TurnSnapshot(
            turn=i,
            user_message=user_msg,
            expected_output_hash=hashlib.sha256(output.encode()).hexdigest()[:12],
            expected_output_preview=output[:100],
            key_assertions=assertions[i]
        ))
        print(f"  [Record] Turn {i}: {output[:60]}...")

    save_conv_snapshot(snap)
    print(f"[ConvSnapshot] {test_id}: recorded {len(snap.turns)} turns")
    return snap

def replay_conversation_snapshot(
    test_id: str,
    model: str = "claude-haiku-4-5-20251001",
    strict_match: bool = False
) -> dict:
    """Replay a conversation and compare each turn to snapshot."""
    snap = load_conv_snapshot(test_id)
    if not snap:
        return {"status": "no_snapshot", "test_id": test_id}

    messages = []
    turn_results = []
    all_pass = True

    for turn_snap in snap.turns:
        messages.append({"role": "user", "content": turn_snap.user_message})
        response = client.messages.create(
            model=model, max_tokens=200, system=snap.system, messages=messages
        )
        current_output = response.content[0].text
        current_hash = hashlib.sha256(current_output.encode()).hexdigest()[:12]
        messages.append({"role": "assistant", "content": current_output})

        # Check assertions
        missing = [a for a in turn_snap.key_assertions if a.lower() not in current_output.lower()]
        hash_match = current_hash == turn_snap.expected_output_hash
        assertions_pass = len(missing) == 0
        turn_pass = (hash_match if strict_match else True) and assertions_pass

        if not turn_pass:
            all_pass = False

        turn_results.append({
            "turn": turn_snap.turn,
            "hash_match": hash_match,
            "assertions_pass": assertions_pass,
            "missing_assertions": missing,
            "status": "pass" if turn_pass else "fail"
        })
        icon = "✓" if turn_pass else "✗"
        print(f"  {icon} Turn {turn_snap.turn}: hash_match={hash_match}, assertions_ok={assertions_pass}")
        if missing:
            print(f"      Missing: {missing}")

    status = "pass" if all_pass else "fail"
    print(f"[ConvSnapshot] {test_id}: {status}")
    return {"status": status, "test_id": test_id, "turns": turn_results}

# Usage: record then replay
SYSTEM = "You are a Python tutor. Give concise answers."

record_conversation_snapshot(
    test_id="python_tutorial_flow",
    conversation=[
        "What is a list in Python?",
        "How do I append to a list?",
        "What's the difference between append and extend?"
    ],
    system=SYSTEM,
    key_assertions_per_turn=[
        ["list", "ordered", "elements"],
        ["append", "("],
        ["append", "extend", "single", "iterable"]
    ]
)

print()
replay_conversation_snapshot("python_tutorial_flow")

# Expected Token Savings: Catches multi-turn regression without manual conversation replay
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/conv_snapshots/
```

### Option 5: Snapshot Test Runner with CI Report

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class TestCase:
    test_id: str
    prompt: str
    system: str
    model: str
    validator: Callable[[str], tuple[bool, str]]  # (output) -> (passed, reason)
    tags: list[str] = field(default_factory=list)

@dataclass
class TestResult:
    test_id: str
    status: str  # pass | fail | error
    duration_ms: float
    output: str
    reason: str
    tags: list[str]

class SnapshotTestRunner:
    def __init__(self, snapshot_dir: str = "/tmp/snapshot_runner"):
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(exist_ok=True)
        self.results: list[TestResult] = []

    def _load_snapshot(self, test_id: str) -> str | None:
        path = self.snapshot_dir / f"{test_id}.txt"
        return path.read_text() if path.exists() else None

    def _save_snapshot(self, test_id: str, output: str):
        (self.snapshot_dir / f"{test_id}.txt").write_text(output)

    def run(self, test: TestCase, update_snapshot: bool = False) -> TestResult:
        t0 = time.time()
        try:
            response = client.messages.create(
                model=test.model, max_tokens=300, system=test.system,
                messages=[{"role": "user", "content": test.prompt}]
            )
            output = response.content[0].text
            duration = (time.time() - t0) * 1000

            existing_snapshot = self._load_snapshot(test.test_id)

            if existing_snapshot is None or update_snapshot:
                self._save_snapshot(test.test_id, output)
                status = "new" if existing_snapshot is None else "updated"
                reason = "Snapshot created" if status == "new" else "Snapshot updated"
            else:
                # Run custom validator
                passed, reason = test.validator(output)
                if not passed:
                    status = "fail"
                else:
                    status = "pass"
                    reason = f"Validator passed: {reason}"

            result = TestResult(test_id=test.test_id, status=status,
                duration_ms=duration, output=output, reason=reason, tags=test.tags)

        except Exception as e:
            result = TestResult(
                test_id=test.test_id, status="error",
                duration_ms=(time.time() - t0) * 1000,
                output="", reason=str(e), tags=test.tags
            )

        self.results.append(result)
        icon = {"pass": "✓", "fail": "✗", "error": "!", "new": "+", "updated": "~"}.get(result.status, "?")
        print(f"  [{icon}] {test.test_id} ({result.duration_ms:.0f}ms): {result.reason[:70]}")
        return result

    def run_suite(self, tests: list[TestCase], update_snapshots: bool = False) -> dict:
        print(f"\n=== Running {len(tests)} snapshot tests ===\n")
        for test in tests:
            self.run(test, update_snapshot=update_snapshots)

        passed = sum(1 for r in self.results if r.status == "pass")
        failed = sum(1 for r in self.results if r.status == "fail")
        errors = sum(1 for r in self.results if r.status == "error")
        new_snaps = sum(1 for r in self.results if r.status in ("new", "updated"))

        print(f"\n=== Results: {passed} pass | {failed} fail | {errors} error | {new_snaps} new/updated ===")
        return {
            "total": len(tests), "passed": passed, "failed": failed,
            "errors": errors, "new": new_snaps,
            "success": failed == 0 and errors == 0
        }

    def generate_report(self, output_path: str = "/tmp/snapshot_report.json"):
        report = {
            "timestamp": time.time(),
            "results": [{"test_id": r.test_id, "status": r.status,
                "duration_ms": round(r.duration_ms), "reason": r.reason,
                "tags": r.tags} for r in self.results]
        }
        Path(output_path).write_text(json.dumps(report, indent=2))
        print(f"Report written: {output_path}")

# Define test suite
def contains_validator(*keywords: str) -> Callable:
    def validate(output: str) -> tuple[bool, str]:
        missing = [k for k in keywords if k.lower() not in output.lower()]
        if missing:
            return False, f"Missing keywords: {missing}"
        return True, f"All keywords present: {list(keywords)}"
    return validate

def min_length_validator(min_chars: int) -> Callable:
    def validate(output: str) -> tuple[bool, str]:
        if len(output) < min_chars:
            return False, f"Output too short: {len(output)} < {min_chars} chars"
        return True, f"Length ok: {len(output)} chars"
    return validate

runner = SnapshotTestRunner()

test_suite = [
    TestCase("format_json", "List 3 colors as JSON array", "",
        "claude-haiku-4-5-20251001", contains_validator("[", "]", "{"), tags=["format"]),
    TestCase("python_types", "Name Python's 4 primitive types",
        "You are a concise tutor.", "claude-haiku-4-5-20251001",
        contains_validator("int", "float", "str", "bool"), tags=["content", "python"]),
    TestCase("haiku_poem", "Write a haiku about code",
        "You are a poet.", "claude-haiku-4-5-20251001",
        min_length_validator(20), tags=["creative"]),
]

summary = runner.run_suite(test_suite, update_snapshots=True)  # First run: create
runner.generate_report()

# Expected Token Savings: CI integration catches regressions automatically before deployment
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/snapshot_runner/ and report JSON
```

### Option 6: LLM-as-Judge Snapshot Evaluation

```python
import anthropic
import json
from dataclasses import dataclass
from pathlib import Path

client = anthropic.Anthropic()

JUDGE_SNAPSHOT_DIR = Path("/tmp/judge_snapshots")
JUDGE_SNAPSHOT_DIR.mkdir(exist_ok=True)

@dataclass
class JudgeSnapshot:
    test_id: str
    prompt: str
    system: str
    reference_output: str
    quality_dimensions: list[str]   # e.g., ["accuracy", "conciseness", "tone"]
    min_scores: dict[str, float]    # dimension -> minimum acceptable score

def save_judge_snapshot(snap: JudgeSnapshot):
    import dataclasses
    path = JUDGE_SNAPSHOT_DIR / f"{snap.test_id}.json"
    path.write_text(json.dumps(dataclasses.asdict(snap), indent=2))

def load_judge_snapshot(test_id: str) -> JudgeSnapshot | None:
    path = JUDGE_SNAPSHOT_DIR / f"{test_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return JudgeSnapshot(**data)

def llm_judge(
    prompt: str,
    reference: str,
    candidate: str,
    dimensions: list[str]
) -> dict[str, float]:
    """Use Claude as judge to score candidate vs reference on each dimension."""
    dims_list = "\n".join(f'  "{d}": <0.0-1.0>' for d in dimensions)
    judge_prompt = f"""You are an objective evaluator. Score how well the candidate answer compares to the reference.

Original question: {prompt}

Reference answer (gold standard):
{reference}

Candidate answer (to evaluate):
{candidate}

Score each dimension from 0.0 (much worse) to 1.0 (equivalent or better):
{dims_list}

Respond ONLY with JSON like: {{"accuracy": 0.9, "conciseness": 0.7}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": judge_prompt}]
    )

    try:
        import re
        match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
        if match:
            scores = json.loads(match.group())
            return {d: float(scores.get(d, 0.5)) for d in dimensions}
    except (json.JSONDecodeError, ValueError):
        pass
    return {d: 0.5 for d in dimensions}

def create_judge_snapshot(
    test_id: str,
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    quality_dimensions: list[str] = None,
    min_scores: dict[str, float] = None
):
    response = client.messages.create(
        model=model, max_tokens=300, system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    ref_output = response.content[0].text
    dims = quality_dimensions or ["accuracy", "completeness", "clarity"]
    mins = min_scores or {d: 0.7 for d in dims}

    snap = JudgeSnapshot(
        test_id=test_id, prompt=prompt, system=system,
        reference_output=ref_output, quality_dimensions=dims, min_scores=mins
    )
    save_judge_snapshot(snap)
    print(f"[JudgeSnapshot] {test_id}: baseline recorded ({len(ref_output)} chars)")
    return snap

def run_judge_snapshot_test(
    test_id: str,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    snap = load_judge_snapshot(test_id)
    if not snap:
        return {"status": "no_snapshot", "test_id": test_id}

    response = client.messages.create(
        model=model, max_tokens=300, system=snap.system,
        messages=[{"role": "user", "content": snap.prompt}]
    )
    candidate = response.content[0].text

    scores = llm_judge(snap.prompt, snap.reference_output, candidate, snap.quality_dimensions)

    failures = {d: s for d, s in scores.items() if s < snap.min_scores.get(d, 0.7)}
    passed = len(failures) == 0
    status = "pass" if passed else "fail"

    print(f"[JudgeSnapshot] {test_id}: {status}")
    for dim, score in scores.items():
        min_score = snap.min_scores.get(dim, 0.7)
        icon = "✓" if score >= min_score else "✗"
        print(f"  {icon} {dim}: {score:.0%} (min: {min_score:.0%})")

    return {"status": status, "test_id": test_id, "scores": scores, "failures": failures}

# Usage
SYSTEM = "You are a concise technical writer."

create_judge_snapshot(
    "explain_recursion",
    "Explain recursion in programming in 2-3 sentences.",
    SYSTEM,
    quality_dimensions=["accuracy", "clarity", "conciseness"],
    min_scores={"accuracy": 0.8, "clarity": 0.75, "conciseness": 0.7}
)

create_judge_snapshot(
    "rest_api_description",
    "What is a REST API? Explain for a junior developer.",
    SYSTEM,
    quality_dimensions=["accuracy", "completeness", "beginner_friendliness"],
    min_scores={"accuracy": 0.85, "completeness": 0.7, "beginner_friendliness": 0.8}
)

print()
run_judge_snapshot_test("explain_recursion")
run_judge_snapshot_test("rest_api_description")

# Expected Token Savings: LLM judge catches semantic regressions exact matching would miss
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/judge_snapshots/
```

## Comparison

| Option | Match Method | Handles Rewording | File Writes | Best Use Case |
|--------|-------------|-------------------|-------------|---------------|
| Exact Snapshot Match | SHA256 hash | No | Yes | Deterministic/templated outputs |
| Semantic Similarity | Jaccard + key phrases | Yes | Yes | Natural language responses |
| Structured Field Snapshot | Schema field assertions | Yes (structure) | No | JSON/structured agent outputs |
| Multi-Turn Conversation | Per-turn hash + assertions | Partial | Yes | Conversational agent flows |
| CI Test Runner | Pluggable validators | Custom | Yes | CI/CD pipeline integration |
| LLM-as-Judge | Claude scoring | Yes | Yes | Quality-sensitive outputs, nuanced regression |
