---
title: "Agent Doesn't Implement Token Efficiency Scoring"
description: "Solutions for measuring how efficiently an agent uses tokens relative to the value it produces — detecting waste, over-generation, and prompt bloat."
tags: [observability, token-efficiency, cost-optimization, metrics]
difficulty: intermediate
---

## Problem

Agents often consume far more tokens than necessary: verbose system prompts, redundant tool results in context, over-generated responses, and unnecessary re-processing of unchanged data. Without efficiency metrics, there's no way to distinguish a $0.50 call that delivered excellent results from a $0.50 call that burned tokens on boilerplate and got nothing useful done.

---

## Solution 1: Output-to-Input Ratio Scorer

Compute a simple efficiency ratio: useful output tokens per input token consumed. Flag calls where the ratio falls outside healthy bounds.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class EfficiencyScore:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    effective_new_tokens: int  # input - cache_read
    output_to_input_ratio: float
    cache_hit_rate: float
    efficiency_grade: str  # A, B, C, D, F
    warnings: list[str]

    @property
    def cost_usd(self) -> float:
        pricing = {
            "claude-haiku-4-5-20251001": (0.80, 4.00, 0.08, 1.00),
            "claude-sonnet-4-6":         (3.00, 15.00, 0.30, 3.75),
            "claude-opus-4-6":           (15.00, 75.00, 1.50, 18.75),
        }
        inp, out, cr, cw = pricing.get(self.model, (3.00, 15.00, 0.30, 3.75))
        return (
            self.input_tokens * inp / 1_000_000
            + self.output_tokens * out / 1_000_000
            + self.cache_read_tokens * cr / 1_000_000
            + self.cache_write_tokens * cw / 1_000_000
        )

EFFICIENCY_THRESHOLDS = {
    # (min_ratio, max_ratio) for healthy output/input
    "summarization": (0.10, 0.40),   # Output should be much shorter than input
    "extraction":    (0.05, 0.25),   # JSON extraction should be brief
    "generation":    (0.50, 2.00),   # Generation can match or exceed input
    "qa":            (0.05, 0.50),   # QA answers should be concise
    "translation":   (0.80, 1.20),   # Translation ~ same length
    "default":       (0.10, 1.50),
}

def score_efficiency(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    task_type: str = "default",
) -> EfficiencyScore:
    effective_new = max(0, input_tokens - cache_read_tokens)
    ratio = output_tokens / max(1, effective_new)
    cache_hit_rate = cache_read_tokens / max(1, input_tokens)

    warnings = []
    min_r, max_r = EFFICIENCY_THRESHOLDS.get(task_type, EFFICIENCY_THRESHOLDS["default"])

    if ratio < min_r:
        warnings.append(f"Output/input ratio {ratio:.2f} too low for {task_type} (min={min_r})")
    if ratio > max_r:
        warnings.append(f"Output/input ratio {ratio:.2f} too high for {task_type} (max={max_r})")
    if effective_new > 10000 and cache_hit_rate < 0.3:
        warnings.append(f"Low cache utilization {cache_hit_rate:.0%} on large input — consider prompt caching")
    if output_tokens > 2000:
        warnings.append(f"High output token count ({output_tokens}) — check for over-generation")
    if effective_new > 50000:
        warnings.append(f"Very large effective input ({effective_new} tokens) — consider context compression")

    # Grade: weighted by ratio health and cache efficiency
    if not warnings:
        grade = "A"
    elif len(warnings) == 1:
        grade = "B"
    elif len(warnings) == 2:
        grade = "C"
    elif len(warnings) == 3:
        grade = "D"
    else:
        grade = "F"

    return EfficiencyScore(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        effective_new_tokens=effective_new,
        output_to_input_ratio=round(ratio, 3),
        cache_hit_rate=round(cache_hit_rate, 3),
        efficiency_grade=grade,
        warnings=warnings,
    )

def tracked_call(
    messages: list,
    model: str = "claude-sonnet-4-6",
    task_type: str = "default",
    **kwargs,
) -> tuple[anthropic.types.Message, EfficiencyScore]:
    response = client.messages.create(
        model=model, max_tokens=1024, messages=messages, **kwargs
    )
    usage = response.usage
    score = score_efficiency(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
        task_type=task_type,
    )
    return response, score

# Test different task types
scenarios = [
    ("Summarize in 2 sentences: " + "The quick brown fox jumps over the lazy dog. " * 20, "summarization"),
    ("What is 2 + 2?", "qa"),
    ("Write a 500-word essay on machine learning.", "generation"),
    ('Extract as JSON {"name": "...", "age": 0}: "John Doe is 35 years old."', "extraction"),
]

for prompt, task_type in scenarios:
    _, score = tracked_call(
        [{"role": "user", "content": prompt}],
        model="claude-haiku-4-5-20251001",
        task_type=task_type,
    )
    print(f"[{score.efficiency_grade}] {task_type}: in={score.input_tokens}, "
          f"out={score.output_tokens}, ratio={score.output_to_input_ratio}, "
          f"cost=${score.cost_usd:.6f}")
    for w in score.warnings:
        print(f"  ⚠ {w}")
```

---

## Solution 2: System Prompt Bloat Analyzer

Detect oversized system prompts and quantify how much of the token budget they consume.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class SystemPromptAnalysis:
    raw_token_count: int
    section_breakdown: dict[str, int]
    bloat_score: float  # 0.0 = lean, 1.0 = bloated
    redundant_patterns: list[str]
    optimization_suggestions: list[str]
    estimated_savings_tokens: int

BLOAT_PATTERNS = [
    (r"(?i)(always|never|must|should|do not|don't)\s+.{0,50}\1", "Repeated directive"),
    (r"\.{20,}", "Excessive padding"),
    (r"(\n\s*){3,}", "Excessive blank lines"),
    (r"(?i)you are a (very |highly |extremely )?(helpful|intelligent|capable|knowledgeable) (AI |assistant|language model)", "Generic AI identity boilerplate"),
    (r"(?i)(please note that|it is important to note|keep in mind that|remember that).{0,100}\.(\s*(please note|it is important|keep in mind|remember))", "Repeated caveats"),
]

def analyze_system_prompt(system_prompt: str, model: str = "claude-haiku-4-5-20251001") -> SystemPromptAnalysis:
    # Count tokens
    token_resp = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": "test"}],
        system=system_prompt,
    )
    # Approximate system prompt tokens
    system_tokens = max(0, token_resp.input_tokens - 5)

    # Section breakdown
    sections: dict[str, int] = {}
    lines = system_prompt.split("\n")
    current_section = "preamble"
    current_lines = []

    for line in lines:
        if re.match(r"^#{1,3}\s+\w+|^[A-Z][A-Z\s]{5,}:", line.strip()):
            if current_lines:
                sections[current_section] = len(" ".join(current_lines).split()) * 4 // 3
            current_section = line.strip()[:40]
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = len(" ".join(current_lines).split()) * 4 // 3

    # Detect redundancy
    redundant = []
    for pattern, label in BLOAT_PATTERNS:
        if re.search(pattern, system_prompt):
            redundant.append(label)

    # Bloat score
    word_count = len(system_prompt.split())
    bloat_score = min(1.0, system_tokens / 2000)  # >2000 tokens = fully bloated

    # Suggestions
    suggestions = []
    if system_tokens > 500:
        suggestions.append(f"System prompt uses {system_tokens} tokens — consider compressing to <300")
    if len(lines) > 50:
        suggestions.append("Long system prompt detected — use hierarchical structure with most important rules first")
    if "always" in system_prompt.lower() and system_prompt.lower().count("always") > 3:
        suggestions.append("Multiple 'always' directives — consolidate into a single rules section")
    if system_prompt.count("You are") > 1:
        suggestions.append("Multiple 'You are' statements — use a single concise identity statement")

    estimated_savings = max(0, system_tokens - 300)  # target: 300 token system prompt

    return SystemPromptAnalysis(
        raw_token_count=system_tokens,
        section_breakdown=sections,
        bloat_score=round(bloat_score, 2),
        redundant_patterns=redundant,
        optimization_suggestions=suggestions,
        estimated_savings_tokens=estimated_savings,
    )

# Test with a bloated system prompt
bloated_prompt = """
You are a very helpful, highly intelligent, and extremely capable AI assistant and language model.
You are designed to be helpful, harmless, and honest in all your interactions.
Always be polite and respectful. Never be rude or offensive.
Always provide accurate information. Never provide false information.
Always be concise when appropriate. Never be unnecessarily verbose.
Please note that you should always maintain a professional tone.
It is important to note that you must always follow the user's instructions.
Keep in mind that you should always provide helpful responses.
Remember that you are an AI assistant here to help users.

## Rules
- Always greet the user warmly
- Never forget to be helpful
- Always remember to be accurate
- Never provide harmful content
- Always be professional
- Never be unprofessional

## Additional Guidelines
You should always try to understand what the user is asking before responding.
It is very important that you always provide the most helpful response possible.
"""

analysis = analyze_system_prompt(bloated_prompt)
print(f"System prompt analysis:")
print(f"  Token count: {analysis.raw_token_count}")
print(f"  Bloat score: {analysis.bloat_score:.0%}")
print(f"  Estimated savings: {analysis.estimated_savings_tokens} tokens")
print(f"  Redundant patterns: {analysis.redundant_patterns}")
print(f"Suggestions:")
for s in analysis.optimization_suggestions:
    print(f"  → {s}")
```

---

## Solution 3: Tool Result Efficiency Monitor

Track how much of each tool result actually gets referenced in subsequent model outputs — identifying wasteful tool result sizes.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class ToolResultEfficiency:
    tool_name: str
    result_tokens: int
    referenced_tokens: int  # estimated tokens from result used in final output
    utilization_rate: float  # referenced / result
    waste_tokens: int
    grade: str

def estimate_reference_overlap(tool_result: str, model_output: str) -> float:
    """Estimate what fraction of tool result content appeared in model output."""
    result_words = set(re.findall(r'\b\w{4,}\b', tool_result.lower()))
    output_words = set(re.findall(r'\b\w{4,}\b', model_output.lower()))
    if not result_words:
        return 0.0
    overlap = result_words & output_words
    return len(overlap) / len(result_words)

def token_count(text: str) -> int:
    """Rough approximation: 4 chars ≈ 1 token."""
    return max(1, len(text) // 4)

def analyze_tool_call_efficiency(
    tool_name: str,
    tool_result: str,
    subsequent_output: str,
) -> ToolResultEfficiency:
    result_tokens = token_count(tool_result)
    utilization = estimate_reference_overlap(tool_result, subsequent_output)
    referenced_tokens = int(result_tokens * utilization)
    waste_tokens = result_tokens - referenced_tokens

    if utilization >= 0.5:
        grade = "A"
    elif utilization >= 0.30:
        grade = "B"
    elif utilization >= 0.15:
        grade = "C"
    elif utilization >= 0.05:
        grade = "D"
    else:
        grade = "F"

    return ToolResultEfficiency(
        tool_name=tool_name,
        result_tokens=result_tokens,
        referenced_tokens=referenced_tokens,
        utilization_rate=round(utilization, 3),
        waste_tokens=waste_tokens,
        grade=grade,
    )

class ToolEfficiencyTracker:
    def __init__(self):
        self._records: list[ToolResultEfficiency] = []

    def track(self, tool_name: str, result: str, output: str) -> ToolResultEfficiency:
        eff = analyze_tool_call_efficiency(tool_name, result, output)
        self._records.append(eff)
        return eff

    def report(self) -> dict:
        if not self._records:
            return {}
        by_tool: dict[str, list[ToolResultEfficiency]] = {}
        for r in self._records:
            by_tool.setdefault(r.tool_name, []).append(r)

        return {
            tool: {
                "avg_utilization": round(sum(r.utilization_rate for r in recs) / len(recs), 3),
                "avg_waste_tokens": round(sum(r.waste_tokens for r in recs) / len(recs)),
                "total_waste_tokens": sum(r.waste_tokens for r in recs),
                "calls": len(recs),
                "worst_grade": min((r.grade for r in recs), key=lambda g: "ABCDF".index(g)),
            }
            for tool, recs in by_tool.items()
        }

tracker = ToolEfficiencyTracker()

# Simulate tool calls with varying efficiency
tool_scenarios = [
    ("search_web", "Tesla Q4 2024 revenue was $25.7B, up 3% YoY. EPS was $0.73.",
     "Tesla reported $25.7B revenue in Q4 2024."),
    ("read_file", "\n".join([f"Line {i}: some content here" for i in range(200)]),
     "The file contains a list of items."),
    ("query_db", '{"user_id": 42, "name": "Alice", "email": "alice@example.com", "plan": "pro", "created": "2024-01-15", "last_login": "2024-12-01", "usage_tokens": 1500000}',
     "User Alice (ID 42) is on the pro plan."),
    ("get_weather", "Temperature: 72°F, Humidity: 65%, Wind: 10mph NE, UV: 5, Pollen: Medium",
     "It's 72°F with 65% humidity."),
]

for tool_name, result, output in tool_scenarios:
    eff = tracker.track(tool_name, result, output)
    print(f"[{eff.grade}] {tool_name}: util={eff.utilization_rate:.0%}, "
          f"result={eff.result_tokens}tok, waste={eff.waste_tokens}tok")

print("\n--- Efficiency Report ---")
import json
print(json.dumps(tracker.report(), indent=2))
```

---

## Solution 4: Per-Turn Token Budget Enforcer with Auto-Compaction Trigger

Monitor token consumption per conversation turn and trigger context compaction before the budget is wasted on stale content.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class TurnBudgetState:
    turn_number: int
    cumulative_input_tokens: int
    cumulative_output_tokens: int
    cumulative_cost_usd: float
    efficiency_trend: list[float]  # output/input per turn
    compaction_triggered: bool = False

PRICING = {"input": 3.00, "output": 15.00}  # per million, sonnet-4-6

def turn_cost(input_tok: int, output_tok: int) -> float:
    return (input_tok * PRICING["input"] + output_tok * PRICING["output"]) / 1_000_000

class TokenBudgetEnforcer:
    def __init__(
        self,
        max_total_cost_usd: float = 0.10,
        max_input_tokens_per_turn: int = 50000,
        compaction_threshold_pct: float = 0.70,
    ):
        self._max_cost = max_total_cost_usd
        self._max_input_per_turn = max_input_tokens_per_turn
        self._compaction_threshold = compaction_threshold_pct
        self._turns: list[TurnBudgetState] = []
        self._total_cost = 0.0
        self._total_input = 0
        self._total_output = 0

    def check_before_call(self, estimated_input_tokens: int) -> tuple[bool, str]:
        if self._total_cost >= self._max_cost:
            return False, f"Total budget exhausted: ${self._total_cost:.4f} >= ${self._max_cost:.4f}"

        if estimated_input_tokens > self._max_input_per_turn:
            return False, (
                f"Turn input estimate {estimated_input_tokens} > limit {self._max_input_per_turn} "
                f"— compact context first"
            )

        cost_at_capacity = self._total_cost / max(0.001, self._max_cost)
        if cost_at_capacity > self._compaction_threshold:
            return False, (
                f"Budget {cost_at_capacity:.0%} consumed — compact before next turn "
                f"(${self._total_cost:.4f} / ${self._max_cost:.4f})"
            )

        return True, "OK"

    def record_turn(self, input_tokens: int, output_tokens: int) -> TurnBudgetState:
        cost = turn_cost(input_tokens, output_tokens)
        self._total_cost += cost
        self._total_input += input_tokens
        self._total_output += output_tokens

        ratio = output_tokens / max(1, input_tokens)
        trend = [(s.efficiency_trend[-1] if s.efficiency_trend else ratio)
                 for s in self._turns[-3:]] + [ratio]

        state = TurnBudgetState(
            turn_number=len(self._turns) + 1,
            cumulative_input_tokens=self._total_input,
            cumulative_output_tokens=self._total_output,
            cumulative_cost_usd=round(self._total_cost, 6),
            efficiency_trend=trend,
        )
        self._turns.append(state)
        return state

    def summary(self) -> dict:
        avg_ratio = (
            sum(t.efficiency_trend[-1] for t in self._turns) / len(self._turns)
            if self._turns else 0
        )
        return {
            "turns": len(self._turns),
            "total_input_tokens": self._total_input,
            "total_output_tokens": self._total_output,
            "total_cost_usd": round(self._total_cost, 6),
            "budget_remaining_usd": round(max(0, self._max_cost - self._total_cost), 6),
            "budget_utilization": round(self._total_cost / self._max_cost, 2),
            "avg_efficiency_ratio": round(avg_ratio, 3),
        }

enforcer = TokenBudgetEnforcer(max_total_cost_usd=0.001, compaction_threshold_pct=0.60)

messages = []
prompts = [
    "What is Python?",
    "Explain list comprehensions with examples.",
    "How does Python's GIL work?",
    "What are async generators?",
    "Explain Python's memory model.",
]

for prompt in prompts:
    messages.append({"role": "user", "content": prompt})
    estimated = client.messages.count_tokens(
        model="claude-haiku-4-5-20251001", messages=messages
    ).input_tokens

    ok, reason = enforcer.check_before_call(estimated)
    if not ok:
        print(f"[BLOCKED] Turn {len(enforcer._turns)+1}: {reason}")
        break

    response = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256, messages=messages
    )
    messages.append({"role": "assistant", "content": response.content[0].text})
    state = enforcer.record_turn(response.usage.input_tokens, response.usage.output_tokens)
    print(f"Turn {state.turn_number}: cost=${turn_cost(response.usage.input_tokens, response.usage.output_tokens):.6f}, "
          f"cumulative=${state.cumulative_cost_usd:.6f}")

import json
print(f"\nSummary: {json.dumps(enforcer.summary(), indent=2)}")
```

---

## Solution 5: Efficiency Regression Detector for Prompt Changes

Alert when a prompt update causes token efficiency to regress — catching prompt rewrites that add bloat without improving output quality.

```python
import anthropic
import difflib
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class PromptVersion:
    version: str
    system_prompt: str
    avg_input_tokens: float
    avg_output_tokens: float
    avg_cost_usd: float
    sample_count: int

    @property
    def tokens_per_dollar(self) -> float:
        return self.avg_output_tokens / max(0.000001, self.avg_cost_usd)

def benchmark_prompt(
    system_prompt: str,
    test_messages: list[list[dict]],
    model: str = "claude-haiku-4-5-20251001",
    version: str = "v1",
) -> PromptVersion:
    total_input = total_output = total_cost = 0

    for messages in test_messages:
        response = client.messages.create(
            model=model, max_tokens=512,
            system=system_prompt, messages=messages
        )
        usage = response.usage
        inp = usage.input_tokens
        out = usage.output_tokens
        cost = (inp * 0.80 + out * 4.00) / 1_000_000  # haiku pricing

        total_input += inp
        total_output += out
        total_cost += cost

    n = len(test_messages)
    return PromptVersion(
        version=version,
        system_prompt=system_prompt,
        avg_input_tokens=round(total_input / n, 1),
        avg_output_tokens=round(total_output / n, 1),
        avg_cost_usd=round(total_cost / n, 6),
        sample_count=n,
    )

def compare_prompt_versions(baseline: PromptVersion, candidate: PromptVersion) -> dict:
    input_delta_pct = (candidate.avg_input_tokens - baseline.avg_input_tokens) / max(1, baseline.avg_input_tokens)
    output_delta_pct = (candidate.avg_output_tokens - baseline.avg_output_tokens) / max(1, baseline.avg_output_tokens)
    cost_delta_pct = (candidate.avg_cost_usd - baseline.avg_cost_usd) / max(0.000001, baseline.avg_cost_usd)

    regressions = []
    if input_delta_pct > 0.20:
        regressions.append(f"Input tokens increased {input_delta_pct:.0%} — prompt likely bloated")
    if cost_delta_pct > 0.20:
        regressions.append(f"Cost increased {cost_delta_pct:.0%} without justified reason")
    if output_delta_pct > 0.50:
        regressions.append(f"Output tokens increased {output_delta_pct:.0%} — over-generation risk")

    # Show prompt diff
    diff = list(difflib.unified_diff(
        baseline.system_prompt.splitlines(),
        candidate.system_prompt.splitlines(),
        lineterm="", n=2
    ))

    return {
        "baseline": baseline.version,
        "candidate": candidate.version,
        "input_token_delta": f"{input_delta_pct:+.0%}",
        "output_token_delta": f"{output_delta_pct:+.0%}",
        "cost_delta": f"{cost_delta_pct:+.0%}",
        "regressions": regressions,
        "recommendation": "REJECT" if regressions else "APPROVE",
        "prompt_diff_lines": len(diff),
    }

# Test messages
test_msgs = [
    [{"role": "user", "content": "What is machine learning?"}],
    [{"role": "user", "content": "Explain neural networks briefly."}],
    [{"role": "user", "content": "What is overfitting?"}],
]

# Baseline: lean prompt
baseline_prompt = "You are an ML tutor. Be concise and accurate."

# Candidate: bloated rewrite
candidate_prompt = """
You are a very helpful, highly knowledgeable, and extremely capable machine learning tutor and AI assistant.
You are designed to always provide the most helpful, accurate, and comprehensive explanations possible.
Always greet the user warmly before responding.
Always be polite, respectful, and professional in all your responses.
Please make sure to always provide detailed examples and explanations.
It is very important that you always cover all aspects of the topic thoroughly.
Never provide incomplete or inaccurate information.
Always remember to be educational and informative.
"""

baseline_v = benchmark_prompt(baseline_prompt, test_msgs, version="v1-lean")
candidate_v = benchmark_prompt(candidate_prompt, test_msgs, version="v2-verbose")

comparison = compare_prompt_versions(baseline_v, candidate_v)
import json
print(json.dumps(comparison, indent=2))
```

---

## Solution 6: Real-Time Efficiency Dashboard with Alerting

Emit efficiency metrics to a live dashboard with threshold-based alerting for ops teams.

```python
import anthropic
import asyncio
import time
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

client = anthropic.AsyncAnthropic()

PRICING = {
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-6":           (15.00, 75.00),
}

@dataclass
class EfficiencyMetric:
    timestamp: float
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    output_per_dollar: float  # output tokens per $1 spent
    tokens_per_ms: float      # output speed

class EfficiencyDashboard:
    def __init__(self, window: int = 50, alert_fn: Optional[Callable] = None):
        self._window = window
        self._metrics: deque[EfficiencyMetric] = deque(maxlen=window)
        self._alert_fn = alert_fn or (lambda msg: print(f"[ALERT] {msg}"))
        self._thresholds = {
            "min_output_per_dollar": 5000,   # tokens/$1
            "max_cost_per_call": 0.01,        # $0.01
            "min_tokens_per_ms": 0.01,        # minimum output speed
        }

    async def tracked_call(
        self, messages: list, model: str = "claude-sonnet-4-6", **kwargs
    ):
        pricing = PRICING.get(model, (3.00, 15.00))
        t0 = time.time()
        response = await client.messages.create(
            model=model, max_tokens=512, messages=messages, **kwargs
        )
        latency_ms = int((time.time() - t0) * 1000)
        usage = response.usage
        cost = (usage.input_tokens * pricing[0] + usage.output_tokens * pricing[1]) / 1_000_000
        output_per_dollar = usage.output_tokens / max(0.000001, cost)
        tokens_per_ms = usage.output_tokens / max(1, latency_ms)

        metric = EfficiencyMetric(
            timestamp=time.time(),
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            output_per_dollar=output_per_dollar,
            tokens_per_ms=tokens_per_ms,
        )
        self._metrics.append(metric)
        self._check_alerts(metric)
        return response

    def _check_alerts(self, m: EfficiencyMetric):
        if m.output_per_dollar < self._thresholds["min_output_per_dollar"]:
            self._alert_fn(
                f"Low efficiency: {m.output_per_dollar:.0f} output tokens/$1 "
                f"(threshold={self._thresholds['min_output_per_dollar']})"
            )
        if m.cost_usd > self._thresholds["max_cost_per_call"]:
            self._alert_fn(
                f"High single-call cost: ${m.cost_usd:.6f} "
                f"(threshold=${self._thresholds['max_cost_per_call']})"
            )

    def snapshot(self) -> dict:
        if not self._metrics:
            return {}
        recent = list(self._metrics)
        return {
            "calls": len(recent),
            "avg_input_tokens": round(sum(m.input_tokens for m in recent) / len(recent), 1),
            "avg_output_tokens": round(sum(m.output_tokens for m in recent) / len(recent), 1),
            "avg_cost_usd": round(sum(m.cost_usd for m in recent) / len(recent), 6),
            "avg_output_per_dollar": round(sum(m.output_per_dollar for m in recent) / len(recent), 0),
            "avg_latency_ms": round(sum(m.latency_ms for m in recent) / len(recent), 0),
            "total_cost_usd": round(sum(m.cost_usd for m in recent), 6),
            "p95_cost_usd": sorted(m.cost_usd for m in recent)[int(len(recent) * 0.95)],
        }

async def main():
    dashboard = EfficiencyDashboard(window=10)

    prompts = [
        ("What is 2+2?", "claude-haiku-4-5-20251001"),
        ("Explain quantum computing in detail with examples.", "claude-sonnet-4-6"),
        ("List 3 Python tips.", "claude-haiku-4-5-20251001"),
        ("Write a 300-word story about a robot.", "claude-sonnet-4-6"),
        ("What is the capital of France?", "claude-haiku-4-5-20251001"),
    ]

    tasks = [
        dashboard.tracked_call(
            [{"role": "user", "content": prompt}],
            model=model,
        )
        for prompt, model in prompts
    ]
    await asyncio.gather(*tasks)

    print("\n=== Efficiency Dashboard Snapshot ===")
    print(json.dumps(dashboard.snapshot(), indent=2))

asyncio.run(main())
```

---

## Comparison

| Solution | What It Measures | Overhead | Actionable Output | Best For |
|---|---|---|---|---|
| Output/Input Ratio Scorer | Token efficiency per call | <1ms | Grade + warnings | Per-call monitoring |
| System Prompt Bloat Analyzer | Prompt token waste | ~100ms (token count API) | Specific suggestions | Prompt optimization |
| Tool Result Efficiency Monitor | Tool result utilization | <1ms | Waste tokens per tool | Tool result sizing |
| Per-Turn Budget Enforcer | Cumulative cost control | <1ms | Hard gate | Cost-bounded agents |
| Prompt Efficiency Regression | Prompt change impact | 3x calls per benchmark | Approve/reject signal | CI/CD prompt gate |
| Real-Time Dashboard | Aggregate metrics + alerts | Async, minimal | Ops visibility | Production monitoring |

**Recommended approach:** Deploy Solution 1 (ratio scorer) on every call as a lightweight always-on metric, Solution 4 (budget enforcer) for cost-sensitive agents, and Solution 5 (regression detector) in CI when system prompts change.
