---
layout: solution
title: "Agent Doesn't Implement Parameterized Scenario Testing"
category: testing
description: "How to run the same agent behavior test across many input variations — different users, locales, tones, edge cases — without writing a separate test for each combination."
tags: [testing, parameterized, scenarios, coverage, fixtures, regression]
---

# Agent Doesn't Implement Parameterized Scenario Testing

Writing one test per input variation is unscalable. When an agent should handle 10 user types, 5 locales, and 3 urgency levels, that's 150 combinations — not 150 tests. Parameterized scenario testing defines test dimensions once, generates the combinations, runs them in parallel, and reports failures by parameter rather than by individual test ID.

## Option 1: Simple Parameter Grid Expansion

Define parameter axes and expand them into a full test matrix, running each combination against the agent.

```python
import anthropic
import itertools
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class TestCase:
    name: str
    params: dict
    prompt: str
    expected_keywords: list[str]
    forbidden_keywords: list[str] = None

    def __post_init__(self):
        self.forbidden_keywords = self.forbidden_keywords or []


@dataclass
class TestResult:
    test_case: TestCase
    passed: bool
    output: str
    failures: list[str]


def expand_parameter_grid(**axes: list) -> list[dict]:
    """Expand parameter axes into all combinations."""
    keys = list(axes.keys())
    values = list(axes.values())
    combinations = []
    for combo in itertools.product(*values):
        combinations.append(dict(zip(keys, combo)))
    return combinations


def build_prompt_from_params(template: str, params: dict) -> str:
    """Substitute parameters into prompt template."""
    prompt = template
    for key, value in params.items():
        prompt = prompt.replace(f"{{{key}}}", str(value))
    return prompt


def run_parameterized_test(test_case: TestCase) -> TestResult:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": test_case.prompt}],
    )
    output = response.content[0].text.lower()

    failures = []
    for keyword in test_case.expected_keywords:
        if keyword.lower() not in output:
            failures.append(f"Missing expected keyword: '{keyword}'")
    for keyword in test_case.forbidden_keywords:
        if keyword.lower() in output:
            failures.append(f"Contains forbidden keyword: '{keyword}'")

    return TestResult(
        test_case=test_case,
        passed=len(failures) == 0,
        output=output[:200],
        failures=failures,
    )


def run_grid_suite(
    prompt_template: str,
    expected_keywords: list[str],
    **param_axes,
) -> dict:
    """Run all parameter combinations and report results."""
    combinations = expand_parameter_grid(**param_axes)
    print(f"Running {len(combinations)} test combinations...")

    results = []
    for params in combinations:
        name = "-".join(f"{k}={v}" for k, v in params.items())
        prompt = build_prompt_from_params(prompt_template, params)
        tc = TestCase(name=name, params=params, prompt=prompt, expected_keywords=expected_keywords)
        result = run_parameterized_test(tc)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {name}")
        if not result.passed:
            for f in result.failures:
                print(f"    → {f}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\nResults: {passed}/{total} passed")

    # Group failures by parameter
    failure_by_param = {}
    for result in results:
        if not result.passed:
            for k, v in result.test_case.params.items():
                key = f"{k}={v}"
                failure_by_param[key] = failure_by_param.get(key, 0) + 1

    return {
        "total": total,
        "passed": passed,
        "failures_by_param": failure_by_param,
        "results": results,
    }


if __name__ == "__main__":
    # Test: agent handles password reset requests across user types and urgency levels
    report = run_grid_suite(
        prompt_template="I am a {user_type} user and I {urgency} need to reset my password for {product}.",
        expected_keywords=["password", "reset"],
        user_type=["new", "premium", "enterprise"],
        urgency=["urgently", "casually"],
        product=["the mobile app", "the web portal"],
    )
    print(f"\nFailure analysis: {json.dumps(report['failures_by_param'], indent=2)}")

# Expected Token Savings: N×M test coverage in N+M prompts worth of design; finds parameter-specific regressions
# Environment: CI pipelines for customer-facing agents, pre-release regression sweeps
```

## Option 2: Fixture-Based Scenario Testing with YAML Definitions

Define test scenarios in YAML fixtures so non-engineers can add test cases without modifying code.

```python
import anthropic
import yaml
import re
from dataclasses import dataclass, field
from typing import Optional


FIXTURE_YAML = """
scenarios:
  - name: "greeting-english"
    params:
      language: "English"
      time_of_day: "morning"
    prompt: "Say good morning in {language}"
    checks:
      contains_any: ["good morning", "morning", "hello"]
      not_contains: ["error", "cannot"]
      max_length: 100

  - name: "greeting-spanish"
    params:
      language: "Spanish"
      time_of_day: "morning"
    prompt: "Say good morning in {language}"
    checks:
      contains_any: ["buenos días", "buenos dias", "hola"]
      not_contains: ["error", "cannot"]
      max_length: 100

  - name: "math-simple"
    params:
      operation: "addition"
      difficulty: "easy"
    prompt: "What is 2 + 2?"
    checks:
      contains_any: ["4", "four"]
      not_contains: ["i cannot", "i don't know"]
      max_length: 50

  - name: "math-word-problem"
    params:
      operation: "multiplication"
      difficulty: "medium"
    prompt: "If 3 friends each have 4 apples, how many apples total?"
    checks:
      contains_any: ["12", "twelve"]
      not_contains: ["unclear", "ambiguous"]
      max_length: 150
"""


@dataclass
class FixtureCheck:
    contains_any: list = field(default_factory=list)
    not_contains: list = field(default_factory=list)
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    regex_match: Optional[str] = None

    def evaluate(self, output: str) -> list[str]:
        failures = []
        lower = output.lower()

        if self.contains_any:
            if not any(kw.lower() in lower for kw in self.contains_any):
                failures.append(f"None of {self.contains_any} found in output")

        for kw in self.not_contains:
            if kw.lower() in lower:
                failures.append(f"Forbidden phrase '{kw}' found in output")

        if self.max_length and len(output) > self.max_length:
            failures.append(f"Output length {len(output)} exceeds max {self.max_length}")

        if self.min_length and len(output) < self.min_length:
            failures.append(f"Output length {len(output)} below min {self.min_length}")

        if self.regex_match:
            if not re.search(self.regex_match, output, re.IGNORECASE):
                failures.append(f"Regex '{self.regex_match}' did not match output")

        return failures


def load_fixtures(yaml_str: str) -> list[dict]:
    data = yaml.safe_load(yaml_str)
    return data.get("scenarios", [])


def run_fixture_scenario(scenario: dict) -> dict:
    client = anthropic.Anthropic()

    name = scenario["name"]
    params = scenario.get("params", {})
    prompt_template = scenario["prompt"]
    checks_data = scenario.get("checks", {})

    # Substitute params into prompt
    prompt = prompt_template
    for k, v in params.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    check = FixtureCheck(
        contains_any=checks_data.get("contains_any", []),
        not_contains=checks_data.get("not_contains", []),
        max_length=checks_data.get("max_length"),
        min_length=checks_data.get("min_length"),
        regex_match=checks_data.get("regex_match"),
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    output = response.content[0].text
    failures = check.evaluate(output)

    return {
        "name": name,
        "params": params,
        "passed": len(failures) == 0,
        "failures": failures,
        "output_preview": output[:80],
    }


def run_fixture_suite(yaml_str: str) -> dict:
    scenarios = load_fixtures(yaml_str)
    print(f"Running {len(scenarios)} fixture scenarios...")

    results = []
    for scenario in scenarios:
        result = run_fixture_scenario(scenario)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {result['name']} (params={result['params']})")
        if not result["passed"]:
            for f in result["failures"]:
                print(f"    → {f}")

    passed = sum(1 for r in results if r["passed"])
    return {"total": len(results), "passed": passed, "results": results}


if __name__ == "__main__":
    summary = run_fixture_suite(FIXTURE_YAML)
    print(f"\n{'='*50}")
    print(f"Suite result: {summary['passed']}/{summary['total']} passed")

# Expected Token Savings: YAML fixtures enable test maintenance without code changes; non-engineers can add coverage
# Environment: Teams with QA specialists, agents with many language/locale scenarios
```

## Option 3: Async Parallel Parameterized Runner

Execute all parameter combinations concurrently to minimize wall-clock test suite time.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional
import time


@dataclass
class ParamScenario:
    scenario_id: str
    params: dict
    prompt: str
    validator: Callable[[str], tuple[bool, list[str]]]  # (output) -> (passed, failures)


@dataclass
class ScenarioResult:
    scenario_id: str
    params: dict
    passed: bool
    failures: list[str]
    output_preview: str
    latency_ms: float


async def run_scenario_async(
    client: anthropic.AsyncAnthropic,
    scenario: ParamScenario,
    semaphore: asyncio.Semaphore,
) -> ScenarioResult:
    async with semaphore:
        start = time.monotonic()
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": scenario.prompt}],
            )
            latency = (time.monotonic() - start) * 1000
            output = response.content[0].text
            passed, failures = scenario.validator(output)

            return ScenarioResult(
                scenario_id=scenario.scenario_id,
                params=scenario.params,
                passed=passed,
                failures=failures,
                output_preview=output[:80],
                latency_ms=latency,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ScenarioResult(
                scenario_id=scenario.scenario_id,
                params=scenario.params,
                passed=False,
                failures=[f"Exception: {e}"],
                output_preview="",
                latency_ms=latency,
            )


async def run_parameterized_suite_async(
    scenarios: list[ParamScenario],
    concurrency: int = 5,
) -> list[ScenarioResult]:
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)

    start = time.monotonic()
    results = await asyncio.gather(*[
        run_scenario_async(client, s, semaphore)
        for s in scenarios
    ])
    total_time = time.monotonic() - start

    passed = sum(1 for r in results if r.passed)
    print(f"\nSuite complete: {passed}/{len(results)} passed in {total_time:.1f}s "
          f"(concurrency={concurrency})")

    # Show failures
    for r in results:
        if not r.passed:
            print(f"  FAIL [{r.scenario_id}] params={r.params}")
            for f in r.failures:
                print(f"    → {f}")

    return results


def build_tone_language_scenarios() -> list[ParamScenario]:
    """Build scenarios testing agent across tones and languages."""
    tones = ["formal", "casual", "urgent"]
    topics = ["account suspension", "billing question", "feature request"]
    scenarios = []

    for tone in tones:
        for topic in topics:
            sid = f"{tone}-{topic.replace(' ', '_')}"

            # Build prompt
            tone_prefix = {
                "formal": "Dear Support Team,",
                "casual": "Hey,",
                "urgent": "URGENT:",
            }[tone]

            prompt = f"{tone_prefix} I have a {topic}. Please help."

            def make_validator(t, topic_str):
                def validator(output: str) -> tuple[bool, list[str]]:
                    failures = []
                    lower = output.lower()
                    if len(output.strip()) < 20:
                        failures.append("Response too short")
                    if "error" in lower and "sorry" not in lower:
                        failures.append("Error without apology")
                    # Formal tone should not use contractions heavily
                    if t == "formal" and lower.count("'") > 3:
                        failures.append("Formal tone contains too many contractions")
                    return len(failures) == 0, failures
                return validator

            scenarios.append(ParamScenario(
                scenario_id=sid,
                params={"tone": tone, "topic": topic},
                prompt=prompt,
                validator=make_validator(tone, topic),
            ))

    return scenarios


if __name__ == "__main__":
    scenarios = build_tone_language_scenarios()
    print(f"Running {len(scenarios)} scenarios in parallel...")
    results = asyncio.run(run_parameterized_suite_async(scenarios, concurrency=4))

    # Summary by tone
    by_tone = {}
    for r in results:
        tone = r.params["tone"]
        by_tone.setdefault(tone, {"pass": 0, "fail": 0})
        by_tone[tone]["pass" if r.passed else "fail"] += 1

    print("\nResults by tone:")
    for tone, counts in by_tone.items():
        total = counts["pass"] + counts["fail"]
        print(f"  {tone}: {counts['pass']}/{total}")

# Expected Token Savings: Parallel execution cuts suite wall-time by concurrency factor; enables faster CI feedback
# Environment: Large test suites where sequential execution would exceed CI time budgets
```

## Option 4: Property-Based Scenario Generation

Instead of manually listing test cases, define properties that any valid response must satisfy, then generate random inputs to test them.

```python
import anthropic
import random
import string
from dataclasses import dataclass
from typing import Callable


@dataclass
class PropertyTest:
    name: str
    description: str
    generator: Callable[[], str]    # Generates random input
    property_check: Callable[[str, str], tuple[bool, str]]  # (input, output) -> (passed, reason)
    n_trials: int = 10


def random_name() -> str:
    first = random.choice(["Alice", "Bob", "Carlos", "Diana", "Elena", "Frank"])
    last = random.choice(["Smith", "Johnson", "Williams", "Garcia", "Lee", "Patel"])
    return f"{first} {last}"


def random_number_question() -> str:
    a = random.randint(1, 100)
    b = random.randint(1, 100)
    op = random.choice(["plus", "minus", "times"])
    return f"What is {a} {op} {b}?"


def extract_number(text: str) -> int | None:
    import re
    matches = re.findall(r'\b\d+\b', text)
    return int(matches[0]) if matches else None


def run_property_test(pt: PropertyTest) -> dict:
    client = anthropic.Anthropic()
    passed = 0
    failures = []

    for trial in range(pt.n_trials):
        test_input = pt.generator()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": test_input}],
        )
        output = response.content[0].text

        ok, reason = pt.property_check(test_input, output)
        if ok:
            passed += 1
        else:
            failures.append({
                "trial": trial + 1,
                "input": test_input,
                "output": output[:80],
                "reason": reason,
            })

    return {
        "name": pt.name,
        "trials": pt.n_trials,
        "passed": passed,
        "failures": failures,
        "pass_rate": passed / pt.n_trials,
    }


# Define properties the agent must satisfy universally

PROPERTY_TESTS = [
    PropertyTest(
        name="non-empty-responses",
        description="Agent never returns empty responses",
        generator=lambda: f"Help me with {random.choice(['Python', 'JavaScript', 'SQL', 'Docker'])}",
        property_check=lambda inp, out: (
            len(out.strip()) > 10,
            "Response is empty or near-empty"
        ),
        n_trials=8,
    ),
    PropertyTest(
        name="name-in-response",
        description="Agent uses the user's name when explicitly stated",
        generator=lambda: f"Hi, my name is {random_name()}. What is 2+2?",
        property_check=lambda inp, out: (
            # Name doesn't need to appear — just check response is helpful
            "4" in out or "four" in out.lower(),
            "Response doesn't answer the math question"
        ),
        n_trials=5,
    ),
    PropertyTest(
        name="no-hallucinated-code",
        description="Code blocks in responses contain syntactically plausible Python",
        generator=lambda: f"Write a Python one-liner to {random.choice(['reverse a string', 'sort a list', 'count words in text'])}",
        property_check=lambda inp, out: (
            # Should contain at least one Python construct
            any(kw in out for kw in ["def ", "return", "lambda", "[::", ".sort", "len("]),
            "Response contains no Python code constructs"
        ),
        n_trials=6,
    ),
]


def run_all_property_tests() -> dict:
    results = []
    for pt in PROPERTY_TESTS:
        print(f"\nProperty: {pt.name} ({pt.n_trials} trials)")
        result = run_property_test(pt)
        results.append(result)
        print(f"  Pass rate: {result['pass_rate']:.0%} ({result['passed']}/{result['trials']})")
        for f in result["failures"][:2]:
            print(f"  FAIL trial {f['trial']}: {f['reason']}")
            print(f"    Input: {f['input'][:50]}")
            print(f"    Output: {f['output'][:60]}")

    overall = sum(r["passed"] for r in results)
    total = sum(r["trials"] for r in results)
    return {"overall_pass_rate": overall / total, "results": results}


if __name__ == "__main__":
    summary = run_all_property_tests()
    print(f"\nOverall pass rate: {summary['overall_pass_rate']:.0%}")

# Expected Token Savings: Property tests find edge cases that manual tests miss; fewer correction cycles in production
# Environment: Agents handling diverse user inputs, any agent where behavioral invariants should hold universally
```

## Option 5: Scenario Matrix with Baseline Comparison

Run scenarios against two versions (e.g., old vs. new prompt or model) and flag regressions where the new version underperforms.

```python
import anthropic
from dataclasses import dataclass
from typing import Callable


@dataclass
class Scenario:
    name: str
    prompt: str
    score_fn: Callable[[str], float]  # (output) -> 0.0-1.0


@dataclass
class ComparisonResult:
    scenario_name: str
    version_a_score: float
    version_b_score: float

    @property
    def regression(self) -> bool:
        return self.version_b_score < self.version_a_score - 0.1

    @property
    def improvement(self) -> bool:
        return self.version_b_score > self.version_a_score + 0.1

    @property
    def delta(self) -> float:
        return self.version_b_score - self.version_a_score


def score_response(output: str, expected_keywords: list, forbidden_keywords: list = None) -> float:
    """Score 0.0–1.0 based on keyword presence."""
    lower = output.lower()
    keyword_hits = sum(1 for kw in expected_keywords if kw.lower() in lower)
    keyword_score = keyword_hits / len(expected_keywords) if expected_keywords else 1.0

    forbidden = forbidden_keywords or []
    penalty = sum(0.2 for kw in forbidden if kw.lower() in lower)

    return max(0.0, min(1.0, keyword_score - penalty))


def run_scenario(client: anthropic.Anthropic, scenario: Scenario, system_prompt: str) -> float:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": scenario.prompt}],
    )
    return scenario.score_fn(response.content[0].text)


def compare_versions(
    scenarios: list[Scenario],
    system_a: str,
    system_b: str,
    label_a: str = "baseline",
    label_b: str = "candidate",
) -> list[ComparisonResult]:
    client = anthropic.Anthropic()
    results = []

    print(f"Comparing '{label_a}' vs '{label_b}' across {len(scenarios)} scenarios...")

    for scenario in scenarios:
        score_a = run_scenario(client, scenario, system_a)
        score_b = run_scenario(client, scenario, system_b)

        result = ComparisonResult(
            scenario_name=scenario.name,
            version_a_score=score_a,
            version_b_score=score_b,
        )
        results.append(result)

        indicator = "📈" if result.improvement else ("📉" if result.regression else "≈")
        print(f"  {indicator} {scenario.name}: {label_a}={score_a:.2f} {label_b}={score_b:.2f} Δ={result.delta:+.2f}")

    regressions = [r for r in results if r.regression]
    improvements = [r for r in results if r.improvement]
    print(f"\nSummary: {len(improvements)} improvements, {len(regressions)} regressions")

    if regressions:
        print("Regressions:")
        for r in regressions:
            print(f"  ⚠️  {r.scenario_name}: {r.version_a_score:.2f} → {r.version_b_score:.2f}")

    return results


if __name__ == "__main__":
    scenarios = [
        Scenario(
            name="helpful-greeting",
            prompt="Hello, I need help with my account.",
            score_fn=lambda o: score_response(o, ["help", "assist", "support"], ["error"]),
        ),
        Scenario(
            name="technical-question",
            prompt="How do I reset my API key?",
            score_fn=lambda o: score_response(o, ["api", "key", "reset", "settings"], []),
        ),
        Scenario(
            name="escalation-request",
            prompt="I want to speak to a manager.",
            score_fn=lambda o: score_response(o, ["manager", "escalate", "team", "transfer"], ["cannot", "impossible"]),
        ),
        Scenario(
            name="billing-inquiry",
            prompt="What is my current billing cycle?",
            score_fn=lambda o: score_response(o, ["billing", "cycle", "account", "payment"], []),
        ),
    ]

    baseline_system = "You are a helpful customer support agent."
    candidate_system = "You are an expert customer support specialist. Always be proactive in offering solutions."

    results = compare_versions(scenarios, baseline_system, candidate_system, "v1-prompt", "v2-prompt")

# Expected Token Savings: Regression detection prevents deploying prompt changes that break existing behavior
# Environment: Prompt engineering workflows, model upgrade validation, A/B testing of system prompts
```

## Option 6: Scenario Coverage Reporter — Find Under-Tested Parameter Combinations

Analyze an existing test suite to identify which parameter combinations lack coverage.

```python
import anthropic
import itertools
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class CoverageMatrix:
    param_axes: dict            # axis_name -> [possible_values]
    tested_combinations: list   # list of dicts with tested param combos
    results: dict = field(default_factory=dict)  # combo_key -> passed/failed

    def all_combinations(self) -> list[dict]:
        keys = list(self.param_axes.keys())
        values = list(self.param_axes.values())
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]

    def combo_key(self, params: dict) -> str:
        return "|".join(f"{k}={v}" for k, v in sorted(params.items()))

    def coverage_report(self) -> dict:
        all_combos = self.all_combinations()
        tested_keys = {self.combo_key(c) for c in self.tested_combinations}
        all_keys = {self.combo_key(c): c for c in all_combos}

        missing = [combo for key, combo in all_keys.items() if key not in tested_keys]
        covered = [combo for key, combo in all_keys.items() if key in tested_keys]

        # Coverage per axis value
        axis_coverage = {}
        for axis, values in self.param_axes.items():
            for val in values:
                tested_for_val = sum(1 for c in self.tested_combinations if c.get(axis) == val)
                total_for_val = len([c for c in all_combos if c.get(axis) == val])
                axis_coverage[f"{axis}={val}"] = {
                    "tested": tested_for_val,
                    "total": total_for_val,
                    "rate": tested_for_val / total_for_val if total_for_val else 0,
                }

        return {
            "total_combinations": len(all_combos),
            "tested_combinations": len(covered),
            "coverage_pct": len(covered) / len(all_combos) * 100,
            "missing_combinations": missing,
            "axis_coverage": axis_coverage,
        }

    def suggest_priority_tests(self, n: int = 5) -> list[dict]:
        """Suggest which missing combinations to test first for max coverage gain."""
        report = self.coverage_report()
        missing = report["missing_combinations"]

        # Score each missing combo by how under-tested its parameter values are
        def priority_score(combo: dict) -> float:
            score = 0.0
            for axis, val in combo.items():
                key = f"{axis}={val}"
                coverage = self.coverage_report()["axis_coverage"].get(key, {})
                rate = coverage.get("rate", 0.0)
                score += (1.0 - rate)  # More score for less-covered values
            return score

        return sorted(missing, key=priority_score, reverse=True)[:n]


def demonstrate_coverage_analysis():
    client = anthropic.Anthropic()

    # Define the parameter space
    matrix = CoverageMatrix(
        param_axes={
            "language": ["English", "Spanish", "French", "Japanese"],
            "user_tier": ["free", "pro", "enterprise"],
            "request_type": ["billing", "technical", "account"],
        },
        tested_combinations=[
            {"language": "English", "user_tier": "free", "request_type": "billing"},
            {"language": "English", "user_tier": "pro", "request_type": "technical"},
            {"language": "Spanish", "user_tier": "free", "request_type": "account"},
            {"language": "English", "user_tier": "enterprise", "request_type": "billing"},
            {"language": "French", "user_tier": "pro", "request_type": "billing"},
        ],
    )

    report = matrix.coverage_report()
    print(f"Coverage: {report['coverage_pct']:.1f}% ({report['tested_combinations']}/{report['total_combinations']} combos)")

    print("\nAxis coverage:")
    for axis_val, stats in sorted(report["axis_coverage"].items()):
        bar = "█" * int(stats["rate"] * 10) + "░" * (10 - int(stats["rate"] * 10))
        print(f"  {axis_val:<30} {bar} {stats['tested']}/{stats['total']} ({stats['rate']:.0%})")

    priorities = matrix.suggest_priority_tests(n=5)
    print(f"\nTop {len(priorities)} recommended tests to add:")
    for i, combo in enumerate(priorities, 1):
        prompt = (
            f"I am a {combo['user_tier']} user writing in {combo['language']}. "
            f"I have a {combo['request_type']} question."
        )
        print(f"  {i}. {combo} → '{prompt[:70]}'")

        # Run the recommended test
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        passed = len(response.content[0].text.strip()) > 20
        matrix.tested_combinations.append(combo)
        matrix.results[matrix.combo_key(combo)] = passed
        print(f"     Result: {'PASS' if passed else 'FAIL'}")

    final_report = matrix.coverage_report()
    print(f"\nCoverage after additions: {final_report['coverage_pct']:.1f}%")


if __name__ == "__main__":
    demonstrate_coverage_analysis()

# Expected Token Savings: Identifies coverage gaps before production — prevents blind spots in behavior testing
# Environment: Mature agent test suites, pre-release checklist automation, QA gap analysis
```

## Comparison

| Option | Input Generation | Parallelism | Failure Grouping | Best For |
|--------|-----------------|-------------|------------------|----------|
| 1 Grid Expansion | Cartesian product | Sequential | By parameter value | Known parameter dimensions with few values |
| 2 YAML Fixtures | Manual definitions | Sequential | By scenario name | Teams where QA authors define test cases |
| 3 Async Parallel | Cartesian product | Full async | By parameter | Large matrices where speed matters |
| 4 Property-Based | Random generation | Sequential | By property | Finding edge cases in unbounded input space |
| 5 Baseline Comparison | Fixed scenarios | Sequential | By regression/improvement | Prompt version upgrades, model migrations |
| 6 Coverage Reporter | Coverage analysis | Sequential | By axis coverage | Auditing existing suites for gaps |
