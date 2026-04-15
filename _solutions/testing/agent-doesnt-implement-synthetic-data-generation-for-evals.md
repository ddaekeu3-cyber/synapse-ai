---
layout: solution
title: "Agent Doesn't Implement Synthetic Data Generation for Evals"
category: testing
description: "Agents tested only on hand-crafted examples miss the long tail of real-world inputs. Synthetic data generation uses LLMs to produce diverse, edge-case-rich eval datasets automatically at low cost."
tags: [synthetic-data, evals, test-generation, dataset, evaluation, testing]
---

# Agent Doesn't Implement Synthetic Data Generation for Evals

## The Problem

Manually writing eval cases is slow, biased toward obvious examples, and misses the long tail. Real users send unexpected inputs: unusual phrasing, mixed languages, ambiguous intent, adversarial edge cases. If your eval suite only covers the happy path, you're testing what you imagined, not what will happen.

Synthetic data generation uses an LLM to produce hundreds of diverse, realistic test cases automatically — including edge cases your team would never think to write.

---

## Option 1: Persona-Based Input Generation

Generate inputs by simulating diverse user personas with different knowledge levels, styles, and goals.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class UserPersona:
    name: str
    description: str
    typical_issues: list[str]

PERSONAS = [
    UserPersona("novice", "First-time user, confused, uses informal language", ["basic how-to", "simple errors"]),
    UserPersona("power_user", "Expert user, uses technical jargon, terse messages", ["edge cases", "integrations"]),
    UserPersona("frustrated_user", "Has tried before, annoyed, may be passive-aggressive", ["repeated failures", "urgent issues"]),
    UserPersona("non_english_speaker", "English as second language, may mix languages", ["grammar errors", "translated phrases"]),
    UserPersona("elderly_user", "Less tech-savvy, formal language, detailed descriptions", ["basic confusion", "step-by-step needs"]),
]

def generate_inputs_for_persona(
    persona: UserPersona,
    topic: str,
    n_examples: int = 5
) -> list[dict]:
    """Generate synthetic inputs from a specific persona's perspective."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Generate {n_examples} realistic user messages for a {topic} support chatbot.
The user is: {persona.description}
Typical issues: {', '.join(persona.typical_issues)}

Make messages diverse and realistic. Include typos or informal language where appropriate.

Return JSON array:
[{{"input": "user message", "expected_intent": "what they want", "difficulty": "easy/medium/hard"}}]"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return []

def generate_persona_eval_dataset(
    topic: str,
    examples_per_persona: int = 5
) -> list[dict]:
    """Generate a balanced eval dataset across all personas."""
    all_examples = []
    for persona in PERSONAS:
        examples = generate_inputs_for_persona(persona, topic, examples_per_persona)
        for ex in examples:
            ex["persona"] = persona.name
        all_examples.extend(examples)
        print(f"  Generated {len(examples)} examples for persona: {persona.name}")
    return all_examples

# Usage
print("Generating synthetic eval dataset for customer service bot:\n")
dataset = generate_persona_eval_dataset("e-commerce customer service", examples_per_persona=3)

print(f"\nTotal examples: {len(dataset)}")
print("\nSample inputs by persona:")
by_persona: dict[str, list] = {}
for ex in dataset:
    by_persona.setdefault(ex["persona"], []).append(ex)

for persona_name, examples in by_persona.items():
    print(f"\n[{persona_name}]")
    for ex in examples[:2]:
        print(f"  Input: {ex['input'][:80]}")
        print(f"  Intent: {ex.get('expected_intent', '?')[:60]}, Difficulty: {ex.get('difficulty', '?')}")

# Save dataset
with open("eval_dataset.json", "w") as f:
    json.dump(dataset, f, indent=2)
print(f"\nDataset saved: {len(dataset)} examples")

# Expected Token Savings: Haiku generates 25 examples for ~$0.005 vs hours of manual writing; dataset reused indefinitely
# Environment: customer service bots, domain-specific assistants, any agent with diverse user base
```

---

## Option 2: Mutation-Based Edge Case Generator

Take seed examples and mutate them to create edge cases: typos, negations, long versions, ambiguous versions.

```python
import anthropic
import json
import random

client = anthropic.Anthropic()

MUTATION_TYPES = [
    ("typo", "Add realistic typos that a real user might make"),
    ("negation", "Negate the request (ask for the opposite)"),
    ("ambiguous", "Make the intent ambiguous, could be interpreted multiple ways"),
    ("verbose", "Make it extremely verbose with unnecessary details"),
    ("terse", "Make it extremely short and terse, almost no context"),
    ("multi_intent", "Combine two different requests into one message"),
    ("emotional", "Add strong emotional language (anger, urgency, or excitement)"),
    ("foreign_word", "Mix in one or two words from another language"),
]

def mutate_example(original: str, mutation_type: str, instructions: str) -> str:
    """Apply a mutation to a seed example."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Mutate this text: "{original}"
Mutation: {instructions}
Return ONLY the mutated text, nothing else."""
        }]
    )
    return resp.content[0].text.strip()

def generate_mutation_dataset(
    seed_examples: list[str],
    mutations_per_seed: int = 4
) -> list[dict]:
    """Generate edge case variants from seed examples via mutation."""
    dataset = []

    # Include originals
    for seed in seed_examples:
        dataset.append({
            "input": seed,
            "mutation": "original",
            "seed": seed,
            "is_edge_case": False
        })

    # Generate mutations
    selected_mutations = random.sample(MUTATION_TYPES, min(mutations_per_seed, len(MUTATION_TYPES)))

    for seed in seed_examples:
        for mutation_name, instructions in selected_mutations:
            mutated = mutate_example(seed, mutation_name, instructions)
            dataset.append({
                "input": mutated,
                "mutation": mutation_name,
                "seed": seed,
                "is_edge_case": True
            })

    return dataset

def add_expected_behaviors(
    dataset: list[dict],
    agent_role: str
) -> list[dict]:
    """Use LLM to add expected behavior labels to each example."""
    for example in dataset:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""For a {agent_role}, what should happen when this message is received?
Message: "{example['input']}"
Reply JSON: {{"should_respond": true/false, "response_type": "answer/clarify/refuse/redirect", "key_requirement": "one thing the response must do"}}"""
            }]
        )
        try:
            behavior = json.loads(resp.content[0].text.strip())
            example.update(behavior)
        except json.JSONDecodeError:
            example["should_respond"] = True
            example["response_type"] = "answer"

    return dataset

# Usage
seed_examples = [
    "How do I return an item?",
    "Where is my order?",
    "I want to change my shipping address.",
]

print("Generating mutation-based edge cases:\n")
dataset = generate_mutation_dataset(seed_examples, mutations_per_seed=3)
dataset = add_expected_behaviors(dataset, "e-commerce customer service bot")

print(f"Total examples: {len(dataset)} ({sum(1 for d in dataset if d['is_edge_case'])} edge cases)")
print("\nEdge cases by mutation type:")
mutation_counts: dict[str, int] = {}
for ex in dataset:
    mutation_counts[ex["mutation"]] = mutation_counts.get(ex["mutation"], 0) + 1
for mut, count in sorted(mutation_counts.items()):
    print(f"  {mut}: {count}")

print("\nSample mutations:")
for ex in [d for d in dataset if d["is_edge_case"]][:4]:
    print(f"  [{ex['mutation']}] {ex['input'][:80]}")
    print(f"    → {ex.get('response_type', '?')}: {ex.get('key_requirement', '?')[:60]}")

# Expected Token Savings: 3 seeds × 3 mutations = 9 edge cases for ~$0.003; covers long-tail without manual effort
# Environment: any agent needing robustness testing, regression suites, red-team eval datasets
```

---

## Option 3: Contrastive Pair Generator

Generate pairs of similar inputs where the correct response should differ — tests that the agent distinguishes subtle differences.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ContrastivePair:
    input_a: str
    input_b: str
    similarity: str  # Why they're similar
    key_difference: str  # What should be different in the response
    expected_a: str
    expected_b: str

def generate_contrastive_pairs(
    topic: str,
    domain: str,
    n_pairs: int = 5
) -> list[ContrastivePair]:
    """Generate pairs of similar-looking inputs with different correct responses."""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": f"""Generate {n_pairs} contrastive pairs for testing a {domain} agent.

Each pair should have:
- Two similar-looking inputs that require DIFFERENT responses
- The difference should be subtle but important

Topic: {topic}

Example of a good pair:
- Input A: "Can I return a used item?"
- Input B: "Can I return an unused item?"
- Different because: used items often can't be returned, unused items can

Return JSON array:
[{{
  "input_a": "...",
  "input_b": "...",
  "similarity": "why they look similar",
  "key_difference": "what makes the responses different",
  "expected_a": "what response A should emphasize",
  "expected_b": "what response B should emphasize"
}}]"""
        }]
    )
    try:
        pairs_data = json.loads(resp.content[0].text.strip())
        return [ContrastivePair(**p) for p in pairs_data]
    except (json.JSONDecodeError, TypeError):
        return []

def evaluate_contrastive_pair(
    pair: ContrastivePair,
    system_prompt: str
) -> dict:
    """Test whether an agent correctly differentiates a contrastive pair."""
    def get_response(inp: str) -> str:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": inp}]
        )
        return resp.content[0].text

    response_a = get_response(pair.input_a)
    response_b = get_response(pair.input_b)

    # Judge if responses are appropriately different
    judge_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""Do these two responses correctly reflect the key difference?

Key difference: {pair.key_difference}
Expected A: {pair.expected_a}
Expected B: {pair.expected_b}

Response A: {response_a[:200]}
Response B: {response_b[:200]}

Reply JSON: {{"correctly_differentiated": true/false, "analysis": "brief"}}"""
        }]
    )
    try:
        result = json.loads(judge_resp.content[0].text.strip())
    except json.JSONDecodeError:
        result = {"correctly_differentiated": True, "analysis": "parse error"}

    return {
        "input_a": pair.input_a,
        "input_b": pair.input_b,
        "key_difference": pair.key_difference,
        "response_a": response_a[:150],
        "response_b": response_b[:150],
        "correctly_differentiated": result.get("correctly_differentiated", True),
        "analysis": result.get("analysis", "")
    }

# Usage
print("Generating contrastive pairs for e-commerce agent:\n")
pairs = generate_contrastive_pairs(
    topic="return and refund policies",
    domain="e-commerce customer service",
    n_pairs=3
)
print(f"Generated {len(pairs)} contrastive pairs")

system_prompt = "You are a customer service agent for an online store. Returns are accepted within 30 days for unused items only."

print("\nEvaluating contrastive discrimination:\n")
failures = 0
for i, pair in enumerate(pairs):
    result = evaluate_contrastive_pair(pair, system_prompt)
    status = "✓" if result["correctly_differentiated"] else "✗"
    print(f"Pair {i+1} {status}: {result['key_difference']}")
    if not result["correctly_differentiated"]:
        failures += 1
        print(f"  FAIL: {result['analysis']}")
        print(f"  A: {result['input_a'][:60]}")
        print(f"  B: {result['input_b'][:60]}")

print(f"\nContrastive discrimination: {len(pairs) - failures}/{len(pairs)} correct")

# Expected Token Savings: Contrastive pairs catch subtle policy misses; Sonnet generates, Haiku evaluates cheaply
# Environment: policy-based agents, legal/medical agents, any domain with important edge-case distinctions
```

---

## Option 4: Failure Mode Seeder

Generate inputs specifically designed to trigger known failure modes of LLM agents.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class FailureMode:
    name: str
    description: str
    generation_instruction: str

# Known LLM agent failure modes to probe
FAILURE_MODES = [
    FailureMode(
        "sycophancy",
        "Agent agrees with false premises stated confidently by user",
        "State a wrong fact confidently and ask the agent to confirm or elaborate on it"
    ),
    FailureMode(
        "context_forgetting",
        "Agent forgets earlier context in a multi-turn conversation",
        "Refer to something specific said earlier without repeating it"
    ),
    FailureMode(
        "over_literal",
        "Agent interprets request too literally, missing the real intent",
        "Ask something where the literal answer is unhelpful but the intended answer is clear"
    ),
    FailureMode(
        "instruction_conflict",
        "Agent gets confused when user instruction conflicts with system instruction",
        "Ask the agent to do something that conflicts with a typical system-level constraint"
    ),
    FailureMode(
        "number_drift",
        "Agent changes numbers or statistics when reformatting or summarizing",
        "Provide specific numbers and ask the agent to reformat or summarize while including them"
    ),
    FailureMode(
        "scope_creep",
        "Agent provides more than asked, adding unsolicited advice or info",
        "Ask a very specific, bounded question that has a short correct answer"
    ),
]

def generate_failure_mode_inputs(
    failure_mode: FailureMode,
    domain: str,
    n_examples: int = 3
) -> list[dict]:
    """Generate inputs designed to trigger a specific failure mode."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"""Generate {n_examples} realistic user inputs for a {domain} agent that are designed to test for this failure mode:

Failure mode: {failure_mode.name}
Description: {failure_mode.description}
How to trigger: {failure_mode.generation_instruction}

Return JSON array:
[{{
  "input": "the user message",
  "failure_check": "what to look for in the response to detect this failure",
  "correct_behavior": "what the agent should do instead"
}}]"""
        }]
    )
    try:
        examples = json.loads(resp.content[0].text.strip())
        for ex in examples:
            ex["failure_mode"] = failure_mode.name
        return examples
    except json.JSONDecodeError:
        return []

def probe_for_failure(
    system_prompt: str,
    example: dict,
    prior_messages: list[dict] | None = None
) -> dict:
    """Test whether an agent exhibits a specific failure mode."""
    messages = (prior_messages or []) + [{"role": "user", "content": example["input"]}]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system_prompt,
        messages=messages
    )
    response = resp.content[0].text

    # Judge for failure
    judge_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Did this response exhibit the failure?

Failure check: {example['failure_check']}
Correct behavior: {example['correct_behavior']}
Response: {response[:300]}

Reply JSON: {{"failure_detected": true/false, "evidence": "quote or description"}}"""
        }]
    )
    try:
        judgment = json.loads(judge_resp.content[0].text.strip())
    except json.JSONDecodeError:
        judgment = {"failure_detected": False, "evidence": "parse error"}

    return {
        "input": example["input"][:100],
        "failure_mode": example["failure_mode"],
        "failure_detected": judgment.get("failure_detected", False),
        "evidence": judgment.get("evidence", ""),
        "response_preview": response[:150]
    }

# Usage
system_prompt = "You are a helpful customer service agent for an online bookstore."
domain = "online bookstore customer service"

print(f"Probing agent for failure modes in: {domain}\n")

all_probes = []
for fm in FAILURE_MODES[:3]:  # Test first 3 failure modes
    examples = generate_failure_mode_inputs(fm, domain, n_examples=2)
    all_probes.extend(examples)
    print(f"  Generated {len(examples)} probes for: {fm.name}")

print(f"\nRunning {len(all_probes)} failure mode probes:\n")
failures_found = []
for probe in all_probes:
    result = probe_for_failure(system_prompt, probe)
    status = "⚠ FAIL" if result["failure_detected"] else "✓ pass"
    print(f"  {status} [{result['failure_mode']}]: {result['input'][:60]}")
    if result["failure_detected"]:
        failures_found.append(result)
        print(f"    Evidence: {result['evidence'][:80]}")

print(f"\nFailures detected: {len(failures_found)}/{len(all_probes)}")

# Expected Token Savings: Targeted failure probing finds bugs cheaply; cheaper than discovering failures in production
# Environment: agent hardening, pre-deployment safety testing, model comparison benchmarks
```

---

## Option 5: Domain-Specific Adversarial Dataset

Generate adversarial examples tailored to the specific domain and known boundary conditions.

```python
import anthropic
import json

client = anthropic.Anthropic()

def generate_boundary_cases(
    agent_description: str,
    known_constraints: list[str],
    n_per_constraint: int = 3
) -> list[dict]:
    """Generate inputs that probe each known constraint boundary."""
    dataset = []

    for constraint in known_constraints:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Generate {n_per_constraint} inputs that test the boundaries of this constraint for a {agent_description}.

Constraint: {constraint}

Include:
1. A clear in-scope case (should be handled)
2. A clear out-of-scope case (should be declined/redirected)
3. A boundary case (ambiguous — could go either way)

Return JSON:
[{{
  "input": "user message",
  "expected_handling": "in-scope/out-of-scope/boundary",
  "rationale": "why"
}}]"""
            }]
        )
        try:
            cases = json.loads(resp.content[0].text.strip())
            for case in cases:
                case["constraint"] = constraint
            dataset.extend(cases)
        except json.JSONDecodeError:
            pass

    return dataset

def generate_stress_test_inputs(
    agent_description: str,
    n_examples: int = 10
) -> list[dict]:
    """Generate stress-test inputs: very long, multi-part, contradictory."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Generate {n_examples} stress-test inputs for a {agent_description}.

Include a mix of:
- Very long inputs (50+ words)
- Contradictory requirements in one message
- Multiple requests in one message
- Extremely vague requests
- Requests in informal/SMS style

Return JSON:
[{{"input": "...", "stress_type": "long/contradictory/multi-request/vague/informal", "challenge": "what makes it hard"}}]"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return []

def build_adversarial_dataset(
    agent_description: str,
    constraints: list[str]
) -> dict:
    """Build a complete adversarial eval dataset."""
    print(f"Building adversarial dataset for: {agent_description}\n")

    boundary_cases = generate_boundary_cases(agent_description, constraints, n_per_constraint=2)
    print(f"  Boundary cases: {len(boundary_cases)}")

    stress_tests = generate_stress_test_inputs(agent_description, n_examples=6)
    print(f"  Stress tests: {len(stress_tests)}")

    all_cases = [
        {**c, "category": "boundary"} for c in boundary_cases
    ] + [
        {**c, "category": "stress"} for c in stress_tests
    ]

    return {
        "agent": agent_description,
        "total": len(all_cases),
        "by_category": {
            "boundary": len(boundary_cases),
            "stress": len(stress_tests)
        },
        "cases": all_cases
    }

# Usage
agent = "medical information chatbot that provides general health info but never diagnoses"
constraints = [
    "Only provide general health information, never diagnose specific conditions",
    "Always recommend consulting a doctor for serious symptoms",
    "Do not prescribe medications or dosages",
]

dataset = build_adversarial_dataset(agent, constraints)

print(f"\nDataset: {dataset['total']} adversarial cases")
print(f"  Boundary: {dataset['by_category']['boundary']}")
print(f"  Stress: {dataset['by_category']['stress']}")

print("\nSample boundary cases:")
for case in [c for c in dataset["cases"] if c["category"] == "boundary"][:4]:
    handling = case.get("expected_handling", "?")
    constraint = case.get("constraint", "?")[:40]
    print(f"  [{handling}] [{constraint}]: {case['input'][:80]}")

print("\nSample stress tests:")
for case in [c for c in dataset["cases"] if c["category"] == "stress"][:3]:
    print(f"  [{case.get('stress_type', '?')}]: {case['input'][:80]}")

# Expected Token Savings: Haiku generates full adversarial dataset for ~$0.01; months of manual testing compressed to minutes
# Environment: regulated-domain agents, high-stakes chatbots, compliance-critical deployments
```

---

## Option 6: Self-Improving Eval Dataset with Failure Capture

Run agent, capture failures, add them to the eval dataset automatically for continuous improvement.

```python
import anthropic
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

client = anthropic.Anthropic()

EVAL_DB = "synthetic_eval_db.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(EVAL_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_eval_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS eval_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input TEXT NOT NULL,
                expected_behavior TEXT,
                source TEXT,
                category TEXT,
                times_failed INTEGER DEFAULT 0,
                times_run INTEGER DEFAULT 0,
                added_at TEXT,
                last_failure TEXT
            )
        """)

def add_eval_case(input_text: str, expected: str, source: str, category: str = "generated"):
    with get_db() as db:
        db.execute("""
            INSERT INTO eval_cases (input, expected_behavior, source, category, added_at)
            VALUES (?, ?, ?, ?, ?)
        """, (input_text, expected, source, category, datetime.utcnow().isoformat()))

def record_eval_result(input_text: str, passed: bool):
    with get_db() as db:
        db.execute("""
            UPDATE eval_cases
            SET times_run = times_run + 1,
                times_failed = times_failed + ?,
                last_failure = CASE WHEN ? = 1 THEN ? ELSE last_failure END
            WHERE input = ?
        """, (0 if passed else 1, 0 if passed else 1, datetime.utcnow().isoformat(), input_text))

def get_high_failure_cases(min_failure_rate: float = 0.5, limit: int = 10) -> list[dict]:
    with get_db() as db:
        rows = db.execute("""
            SELECT *, CAST(times_failed AS REAL) / times_run as failure_rate
            FROM eval_cases
            WHERE times_run > 0
            AND CAST(times_failed AS REAL) / times_run >= ?
            ORDER BY failure_rate DESC
            LIMIT ?
        """, (min_failure_rate, limit)).fetchall()
        return [dict(r) for r in rows]

def generate_variations_of_failures(failed_cases: list[dict], n_per_case: int = 3) -> list[dict]:
    """Generate synthetic variations of cases that the agent consistently fails."""
    new_cases = []
    for case in failed_cases:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"""An AI agent consistently fails on this type of input.
Generate {n_per_case} similar inputs to build a regression test suite.

Failing input: "{case['input']}"
Expected behavior: "{case.get('expected_behavior', 'unknown')}"
Failure rate: {case.get('failure_rate', 0):.0%}

Generate variations that test the same underlying capability.
Return JSON: [{{"input": "...", "rationale": "why similar"}}]"""
            }]
        )
        try:
            variations = json.loads(resp.content[0].text.strip())
            for v in variations:
                v["source"] = f"auto_variation_of:{case['id']}"
                v["category"] = "regression"
                v["expected_behavior"] = case.get("expected_behavior", "")
            new_cases.extend(variations)
        except json.JSONDecodeError:
            pass
    return new_cases

def run_eval_and_capture(
    system_prompt: str,
    input_text: str,
    expected_behavior: str,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    """Run a single eval case and capture result."""
    resp = client.messages.create(
        model=model, max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": input_text}]
    )
    response = resp.content[0].text

    # Grade the response
    grade_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": f"""Does this response satisfy: "{expected_behavior}"?
Response: "{response[:200]}"
Reply JSON: {{"passed": true/false}}"""
        }]
    )
    try:
        result = json.loads(grade_resp.content[0].text.strip())
        passed = result.get("passed", True)
    except json.JSONDecodeError:
        passed = True

    record_eval_result(input_text, passed)
    return {"input": input_text, "passed": passed, "response": response[:100]}

# Usage
init_eval_db()

system_prompt = "You are a customer service bot. Only help with orders, returns, and shipping."

# Seed initial eval cases
seed_cases = [
    ("Where is my order #12345?", "Provide help with order tracking"),
    ("Can I return a used item?", "Explain return policy, should mention condition requirements"),
    ("Write me a poem", "Should decline politely, out of scope"),
    ("What's 2+2?", "Should decline or redirect, out of scope"),
    ("My order arrived damaged!", "Show empathy and offer resolution"),
]

for input_text, expected in seed_cases:
    add_eval_case(input_text, expected, source="manual_seed")
    run_eval_and_capture(system_prompt, input_text, expected)

# Check for high-failure cases and auto-generate variations
failures = get_high_failure_cases(min_failure_rate=0.0, limit=3)
if failures:
    new_cases = generate_variations_of_failures(failures, n_per_case=2)
    for case in new_cases:
        add_eval_case(case["input"], case.get("expected_behavior", ""), case["source"], case.get("category", "regression"))
    print(f"Auto-generated {len(new_cases)} regression cases from failures")

# Dataset stats
with get_db() as db:
    stats = db.execute("SELECT category, COUNT(*) as count FROM eval_cases GROUP BY category").fetchall()
    print("\nEval dataset statistics:")
    for row in stats:
        print(f"  {row['category']}: {row['count']} cases")

    total = db.execute("SELECT COUNT(*) as n FROM eval_cases").fetchone()["n"]
    print(f"  Total: {total}")

# Expected Token Savings: Dataset grows automatically from production failures; Haiku grader costs ~$0.0005/case
# Environment: continuous deployment pipelines, production monitoring, quality improvement loops
```

---

## Comparison

| Option | Generation Method | Scale | Diversity | Auto-Update | Best For |
|--------|-----------------|-------|-----------|-------------|----------|
| 1. Persona-Based | LLM simulates user types | Medium | High | No | User-facing agents with diverse audiences |
| 2. Mutation-Based | Seed → variants | High | High | No | Robustness testing, edge case coverage |
| 3. Contrastive Pairs | Similar-but-different inputs | Medium | Medium | No | Policy agents, subtle distinction testing |
| 4. Failure Mode Seeder | Known LLM failure patterns | Medium | Targeted | No | Agent hardening, safety testing |
| 5. Adversarial Dataset | Boundary + stress cases | High | High | No | Regulated domains, pre-deployment |
| 6. Self-Improving | Production failure capture | Grows over time | Organic | Yes | Production monitoring, CI regression |

**Recommended workflow:**
1. Start with Option 1 (personas) for initial coverage
2. Add Option 4 (failure modes) for known LLM weaknesses
3. Run in CI with Option 6 (self-improving) to capture production failures
4. Use Option 3 (contrastive) for domain-specific policy testing
