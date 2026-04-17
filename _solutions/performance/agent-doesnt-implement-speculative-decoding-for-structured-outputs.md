---
title: "Agent Doesn't Implement Speculative Decoding for Structured Outputs"
description: "Agents that generate structured outputs (JSON, YAML, tool call schemas) token-by-token pay full generation cost even when large portions of the output are predictable template content. Implement speculative template pre-filling that skips generation for known structural tokens, fills only the variable portions via constrained generation, and measures token savings versus full generation."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-decoding-for-structured-outputs
tags: [speculative-decoding, structured-output, template-prefill, constrained-generation, json-generation, token-efficiency]
symptoms:
  - "Generating a 500-token JSON response when 400 tokens are fixed schema structure"
  - "Full generation cost paid for tool call schemas where only argument values vary"
  - "No mechanism to pre-fill known structural tokens and generate only variable fields"
  - "Response latency dominated by generating boilerplate JSON braces and field names"
  - "Cannot measure what fraction of generated tokens are structural vs. content tokens"
---

## Why This Happens

Autoregressive LLMs generate one token at a time, paying equal cost for every token — including structural tokens like `{`, `"tool_name":`, `}` that are fully determined by the schema. When the output structure is known in advance (a JSON object with fixed field names, a tool call with a known schema), those structural tokens can be pre-filled without generation. Only the variable portions (argument values, content fields) require actual sampling. This technique — template pre-filling or speculative constrained generation — reduces effective generation length proportionally to the fraction of structural tokens in the output.

## Solution 1: Structured Output Template Analyzer

```python
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class TokenType(str, Enum := __import__("enum").Enum):
    STRUCTURAL = "structural"   # fixed schema tokens — can be pre-filled
    VARIABLE = "variable"       # content tokens — must be generated


@dataclass
class OutputTemplateSlot:
    name: str
    slot_type: str              # "string" | "number" | "boolean" | "array" | "object"
    placeholder: str            # e.g., "__SLOT_user_name__"
    required: bool = True
    max_tokens: Optional[int] = None


@dataclass
class StructuredOutputTemplate:
    name: str
    template_str: str           # template with slot placeholders
    slots: List[OutputTemplateSlot] = field(default_factory=list)
    structural_tokens_estimate: int = 0
    variable_tokens_max: int = 0

    def structural_fraction(self) -> float:
        total = self.structural_tokens_estimate + self.variable_tokens_max
        if total == 0:
            return 0.0
        return self.structural_tokens_estimate / total


class StructuredOutputTemplateAnalyzer:
    """
    Analyzes a structured output schema to identify structural (pre-fillable)
    vs. variable (must-generate) token portions.
    """

    CHARS_PER_TOKEN = 4

    def analyze_json_schema(self, schema: dict, example_values: dict) -> StructuredOutputTemplate:
        """
        Creates a template from a JSON schema with known field names.
        Structural tokens are the field names and delimiters.
        Variable tokens are the values.
        """
        structural_chars = 0
        variable_chars = 0
        slots = []

        def estimate(obj, depth=0):
            nonlocal structural_chars, variable_chars
            if isinstance(obj, dict):
                structural_chars += 2  # { }
                for k, v in obj.items():
                    structural_chars += len(f'"{k}": ')
                    if isinstance(v, str):
                        placeholder = f"__SLOT_{k}__"
                        estimated_len = len(example_values.get(k, "placeholder_value"))
                        variable_chars += estimated_len
                        slots.append(OutputTemplateSlot(
                            name=k,
                            slot_type="string",
                            placeholder=placeholder,
                            max_tokens=max(10, estimated_len // self.CHARS_PER_TOKEN + 5),
                        ))
                    elif isinstance(v, (int, float)):
                        variable_chars += 5
                        slots.append(OutputTemplateSlot(name=k, slot_type="number", placeholder=f"__SLOT_{k}__"))
                    else:
                        estimate(v, depth + 1)
            elif isinstance(obj, list):
                structural_chars += 2  # [ ]
                for item in obj:
                    estimate(item, depth + 1)

        estimate(schema)
        template_str = json.dumps(schema, indent=2)
        for slot in slots:
            template_str = template_str.replace(f'"{slot.name}"', f'"{slot.name}"', 1)

        return StructuredOutputTemplate(
            name="analyzed_template",
            template_str=template_str,
            slots=slots,
            structural_tokens_estimate=structural_chars // self.CHARS_PER_TOKEN,
            variable_tokens_max=variable_chars // self.CHARS_PER_TOKEN,
        )
```

## Solution 2: Template Pre-Filler

```python
import json
from typing import Any, Dict, Optional


class StructuredOutputTemplatePrefiller:
    """
    Pre-fills structural portions of a template and returns a prompt
    that asks the LLM to complete only the variable slots.
    This reduces the effective generation length.
    """

    SLOT_PATTERN = re.compile(r"__SLOT_(\w+)__")

    def build_completion_prompt(
        self,
        template: StructuredOutputTemplate,
        context: str,
    ) -> str:
        """
        Returns a prompt where the structural template is already filled in
        and the LLM is asked to provide only the slot values.
        """
        slot_instructions = "\n".join(
            f"- {slot.name} ({slot.slot_type}): provide a value"
            + (f" (max ~{slot.max_tokens} tokens)" if slot.max_tokens else "")
            for slot in template.slots
        )

        return (
            f"{context}\n\n"
            f"Complete the following JSON by providing values for these fields only:\n"
            f"{slot_instructions}\n\n"
            f"Return ONLY a JSON object with these keys and nothing else:\n"
            f'{{{", ".join(repr(s.name) + ": <value>" for s in template.slots)}}}'
        )

    def fill_template(
        self,
        template: StructuredOutputTemplate,
        slot_values: Dict[str, Any],
    ) -> str:
        """
        Fills a template with provided slot values to produce the final output.
        """
        result = template.template_str
        for slot in template.slots:
            value = slot_values.get(slot.name)
            if value is None:
                value = "" if slot.slot_type == "string" else "null"
            if slot.slot_type == "string":
                value = json.dumps(str(value))
            else:
                value = json.dumps(value)
            result = result.replace(f'"{slot.placeholder}"', value)
            result = result.replace(slot.placeholder, str(value).strip('"'))
        return result

    def extract_slot_values(self, llm_response: str) -> Dict[str, Any]:
        """Parse the LLM's slot-value-only response."""
        try:
            cleaned = llm_response.strip()
            if not cleaned.startswith("{"):
                start = cleaned.find("{")
                if start >= 0:
                    cleaned = cleaned[start:]
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}
```

## Solution 3: Constrained Generation Budget Tracker

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class GenerationBudgetRecord:
    template_name: str
    structural_tokens: int
    variable_tokens_budgeted: int
    variable_tokens_actual: Optional[int] = None
    total_tokens_saved: Optional[int] = None
    latency_ms: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    def savings_fraction(self) -> float:
        if self.total_tokens_saved is None:
            return 0.0
        total = self.structural_tokens + (self.variable_tokens_actual or 0)
        return self.total_tokens_saved / max(total, 1)


class GenerationBudgetTracker:
    """
    Tracks token savings from template pre-filling across multiple
    structured output generations.
    """

    def __init__(self):
        self._records: List[GenerationBudgetRecord] = []

    def record(self, rec: GenerationBudgetRecord) -> None:
        self._records.append(rec)

    def summary(self, last_n: int = 100) -> dict:
        recent = self._records[-last_n:]
        if not recent:
            return {"runs": 0}

        total_saved = sum(r.total_tokens_saved or 0 for r in recent)
        avg_savings_pct = (
            sum(r.savings_fraction() for r in recent) / len(recent) * 100
        )

        return {
            "runs": len(recent),
            "total_tokens_saved": total_saved,
            "avg_savings_pct": round(avg_savings_pct, 1),
            "avg_latency_ms": round(
                sum(r.latency_ms or 0 for r in recent) / len(recent), 1
            ),
        }
```

## Solution 4: Template-Based Generation Orchestrator

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class TemplatePrefillGenerationOrchestrator:
    """
    Orchestrates the two-step structured generation:
    1. Build a slot-completion prompt (structural pre-fill)
    2. Call LLM for slot values only (minimal generation)
    3. Fill template with slot values (zero additional tokens)
    """

    def __init__(
        self,
        prefiller: StructuredOutputTemplatePrefiller,
        budget_tracker: GenerationBudgetTracker,
    ):
        self._prefiller = prefiller
        self._tracker = budget_tracker

    async def generate(
        self,
        template: StructuredOutputTemplate,
        context: str,
        llm_fn: Callable[[str], str],
        max_tokens_override: Optional[int] = None,
    ) -> dict:
        start = time.time()

        # Build minimal completion prompt
        completion_prompt = self._prefiller.build_completion_prompt(template, context)

        # LLM generates only slot values — much shorter than full output
        slot_max_tokens = max_tokens_override or (
            sum(s.max_tokens or 20 for s in template.slots) + 50
        )
        llm_response = await llm_fn(completion_prompt)
        slot_values = self._prefiller.extract_slot_values(llm_response)

        # Fill template with slot values (no LLM call needed)
        filled = self._prefiller.fill_template(template, slot_values)

        latency_ms = (time.time() - start) * 1000

        # Estimate token savings
        actual_gen_tokens = len(llm_response) // 4
        full_gen_tokens = len(filled) // 4
        saved = full_gen_tokens - actual_gen_tokens

        rec = GenerationBudgetRecord(
            template_name=template.name,
            structural_tokens=template.structural_tokens_estimate,
            variable_tokens_budgeted=template.variable_tokens_max,
            variable_tokens_actual=actual_gen_tokens,
            total_tokens_saved=max(0, saved),
            latency_ms=round(latency_ms, 2),
        )
        self._tracker.record(rec)

        return {
            "output": filled,
            "slot_values": slot_values,
            "tokens_saved_est": max(0, saved),
            "latency_ms": round(latency_ms, 2),
        }
```

## Solution 5: Template Registry

```python
from typing import Dict, List, Optional


class StructuredOutputTemplateRegistry:
    """
    Stores named templates for common structured outputs.
    Templates are reused across requests — no per-request template analysis.
    """

    def __init__(self):
        self._templates: Dict[str, StructuredOutputTemplate] = {}

    def register(self, template: StructuredOutputTemplate) -> None:
        self._templates[template.name] = template

    def get(self, name: str) -> Optional[StructuredOutputTemplate]:
        return self._templates.get(name)

    def list_templates(self) -> List[dict]:
        return [
            {
                "name": t.name,
                "slots": len(t.slots),
                "structural_fraction": round(t.structural_fraction(), 3),
                "structural_tokens": t.structural_tokens_estimate,
            }
            for t in self._templates.values()
        ]
```

## Solution 6: Template Generation Dashboard

```python
import time


class TemplatePrefillGenerationDashboard:
    """
    Reports token savings from template pre-filling across
    all template types and recent runs.
    """

    def __init__(
        self,
        registry: StructuredOutputTemplateRegistry,
        tracker: GenerationBudgetTracker,
    ):
        self._registry = registry
        self._tracker = tracker

    def render(self) -> dict:
        budget_summary = self._tracker.summary(last_n=200)
        templates = self._registry.list_templates()

        return {
            "generated_at": time.time(),
            "registered_templates": len(templates),
            "template_details": templates,
            "generation_budget_summary": budget_summary,
        }
```

## Comparison

| Approach | Schema Analysis | Structural Pre-fill | Slot-Only Generation | Savings Tracking | Template Registry |
|---|---|---|---|---|---|
| StructuredOutputTemplateAnalyzer | Yes (JSON schema) | No | No | No | No |
| StructuredOutputTemplatePrefiller | No | Yes | Yes (prompt) | No | No |
| GenerationBudgetTracker | No | No | No | Yes | No |
| TemplatePrefillGenerationOrchestrator | No | Via prefiller | Via prefiller | Via tracker | No |
| StructuredOutputTemplateRegistry | No | No | No | No | Yes |
| TemplatePrefillGenerationDashboard | No | No | No | No | Yes |

**Best for production**: Pre-filling is most valuable when structural tokens exceed 60% of the total output — measure `structural_fraction()` per template to identify candidates. Tool call schemas (fixed argument names, fixed JSON structure) are the best targets: the argument names are always pre-fillable. Avoid this pattern for outputs where structure is itself variable (free-form JSON, dynamic schemas) — the overhead of failed slot extraction exceeds the savings. Cache compiled templates in `StructuredOutputTemplateRegistry` at startup — template analysis is expensive and should not happen per-request.
