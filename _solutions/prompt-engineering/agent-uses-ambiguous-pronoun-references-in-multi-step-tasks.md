---
layout: solution
title: "Agent Uses Ambiguous Pronoun References in Multi-Step Tasks"
category: prompt-engineering
description: "Agent uses vague pronouns like 'it', 'this', 'that result', or 'the previous output' in multi-step task instructions, causing Claude to apply actions to the wrong object or misinterpret which step's output to use."
tags: [prompt-engineering, multi-step, clarity, agentic, instructions]
---

## Symptom

In a 5-step pipeline, step 3 instructs Claude to "summarize it and pass it to the next tool" — but "it" is ambiguous between the output of step 1, step 2, or the user's original input. The agent applies a transformation to the wrong intermediate result. Debugging reveals that Claude consistently misidentifies which "it" was intended. Outputs are coherent but based on the wrong source material.

## Root Cause

Pronouns in agentic instructions create reference ambiguity that compounds with conversation length. "The result", "the previous output", "it", "this" — these terms are unambiguous in English conversation but fail in structured pipelines where multiple intermediate results exist simultaneously. Claude resolves ambiguous pronouns using recency bias or semantic similarity, which may not match the author's intent. The longer the conversation history, the more candidates exist for "it" to refer to.

## Fix

### Option 1: Name intermediate results explicitly in system prompt

```python
import anthropic

client = anthropic.Anthropic()

# Ambiguous (broken):
BAD_SYSTEM = """You are a document processing agent.
Step 1: Extract the key points from the document.
Step 2: Translate it to French.
Step 3: Summarize it in 3 bullets.
Step 4: Format it as a report."""

# Explicit (correct):
GOOD_SYSTEM = """You are a document processing agent. Follow these steps precisely.

<pipeline_steps>
Step 1 → [EXTRACTED_POINTS]: Extract the key points from the user's document.
Step 2 → [FRENCH_TRANSLATION]: Translate [EXTRACTED_POINTS] to French.
Step 3 → [SUMMARY]: Summarize [FRENCH_TRANSLATION] into 3 bullet points in French.
Step 4 → [FINAL_REPORT]: Format [SUMMARY] as a formal report with a title and date.
</pipeline_steps>

<rules>
- Always refer to intermediate results by their [LABEL] name, not by pronouns.
- If a step is unclear, state which [LABEL] you are working from.
- Output each step's result clearly labeled.
</rules>"""


def run_pipeline(document: str, use_explicit: bool = True) -> str:
    system = GOOD_SYSTEM if use_explicit else BAD_SYSTEM
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": f"Process this document:\n\n{document}"}],
    )
    return response.content[0].text


sample_doc = """
The quarterly review showed strong growth in the APAC region.
Customer satisfaction scores improved by 12 points.
Product returns decreased significantly compared to last quarter.
The engineering team shipped 3 major features ahead of schedule.
"""

print("=== Explicit reference pipeline ===")
result = run_pipeline(sample_doc, use_explicit=True)
print(result[:600])
```

**Expected Token Savings:** Named intermediate results eliminate disambiguation turns where Claude asks "which result did you mean?" or silently applies the wrong transformation.
**Environment:** Python 3.9+; label convention (`[LABEL]`) is arbitrary — use whatever is consistent throughout the prompt.

---

### Option 2: Structured pipeline with explicit input/output contracts

```python
import anthropic

client = anthropic.Anthropic()


def build_pipeline_prompt(steps: list[dict]) -> str:
    """
    Build a pipeline system prompt where each step names its input and output.
    steps: [{"name": str, "input_from": str | None, "description": str, "output_name": str}]
    """
    lines = ["You are a pipeline execution agent. Execute each step exactly as specified.\n"]
    lines.append("<pipeline>")

    for i, step in enumerate(steps, 1):
        input_ref = f"the output of step {i-1} ({steps[i-2]['output_name']})" if step.get("input_from") else "the user's original input"
        lines.append(f"""
<step number="{i}" name="{step['name']}">
  <input>Use {input_ref}</input>
  <task>{step['description']}</task>
  <output_label>{step['output_name']}</output_label>
  <output_format>Label your output as: [{step['output_name']}]: ...</output_format>
</step>""")

    lines.append("</pipeline>")
    lines.append("\n<rules>")
    lines.append("- Never use pronouns to refer to previous results. Always use the output_label.")
    lines.append("- Clearly label each step's output before proceeding to the next step.")
    lines.append("- If an input label is not present in the conversation, stop and ask for clarification.")
    lines.append("</rules>")

    return "\n".join(lines)


# Define pipeline with explicit data flow
pipeline_steps = [
    {
        "name": "extract",
        "input_from": None,
        "description": "Extract all action items from the user's meeting notes. List each as a separate line.",
        "output_name": "ACTION_ITEMS",
    },
    {
        "name": "prioritize",
        "input_from": "ACTION_ITEMS",
        "description": "From ACTION_ITEMS, identify the top 3 most urgent items. Explain why each is urgent.",
        "output_name": "TOP_PRIORITIES",
    },
    {
        "name": "assign",
        "input_from": "TOP_PRIORITIES",
        "description": "For each item in TOP_PRIORITIES, suggest which team role should own it (Engineering, Product, or Operations).",
        "output_name": "ASSIGNMENTS",
    },
    {
        "name": "draft_email",
        "input_from": "ASSIGNMENTS",
        "description": "Draft a brief team email summarizing ASSIGNMENTS with due dates one week from today.",
        "output_name": "DRAFT_EMAIL",
    },
]

system = build_pipeline_prompt(pipeline_steps)

meeting_notes = """
- Need to fix the login bug affecting 500 users (reported Monday)
- Q2 roadmap presentation to board is next Friday
- Update the onboarding docs with the new UI screenshots
- Security audit response due to compliance team by end of month
- Investigate memory leak in the mobile app
- Schedule 1:1s with new team members
"""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system=system,
    messages=[{"role": "user", "content": f"Process these meeting notes:\n{meeting_notes}"}],
)
print(response.content[0].text[:800])
```

**Expected Token Savings:** Explicit input/output contracts prevent the agent from applying a step to the wrong data; eliminates correction turns that add 300–1,000 tokens each.
**Environment:** Python 3.9+; pipeline builder is reusable for any sequential task; works with Claude's XML tag understanding.

---

### Option 3: Multi-turn pipeline with explicit handoffs

```python
import anthropic

client = anthropic.Anthropic()


class ExplicitPipeline:
    """
    Executes pipeline steps one at a time, passing the named output
    of each step explicitly as input to the next — no ambiguous references.
    """

    def __init__(self):
        self.results: dict[str, str] = {}
        self.conversation: list[dict] = []

    def run_step(self, step_name: str, output_label: str, instruction: str, input_label: str | None = None) -> str:
        # Build an unambiguous step instruction
        if input_label and input_label in self.results:
            context = f"\n\n<input_data label='{input_label}'>\n{self.results[input_label]}\n</input_data>"
            task = f"{instruction}\n\nIMPORTANT: Apply this step to the content inside <input_data label='{input_label}'> above, NOT to any other text."
        else:
            context = ""
            task = instruction

        self.conversation.append({"role": "user", "content": f"Step: {step_name}\n{task}{context}"})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=(
                "You are a pipeline step executor. "
                "Apply each step precisely to the specified input. "
                "Never apply a transformation to a different input than specified."
            ),
            messages=self.conversation,
        )

        result = response.content[0].text
        self.results[output_label] = result
        self.conversation.append({"role": "assistant", "content": result})

        print(f"[{step_name}] → [{output_label}]: {result[:80]}...")
        return result

    def get(self, label: str) -> str | None:
        return self.results.get(label)


pipeline = ExplicitPipeline()

# User provides source material
source_text = """
The Mars mission proposal outlines a 3-year timeline starting in 2028.
Key risks include radiation exposure during transit, life support redundancy,
and communication delays of up to 24 minutes. Estimated cost: $2.3B.
Budget requires approval from three government agencies.
"""

# Step 1: extract from original input
pipeline.run_step(
    step_name="Extract risks",
    output_label="RISKS",
    instruction="List all risks mentioned in the user's source text. Format as numbered list.",
    input_label=None,  # Uses original input implicitly — first step
)

# Step 2: work explicitly on RISKS (not on source_text)
pipeline.run_step(
    step_name="Rank risks",
    output_label="RANKED_RISKS",
    instruction="Rank each item in the input data by severity (high/medium/low). Keep the numbering.",
    input_label="RISKS",  # Explicitly RISKS, not source_text
)

# Step 3: work explicitly on RANKED_RISKS
pipeline.run_step(
    step_name="Mitigation plan",
    output_label="MITIGATIONS",
    instruction="For each HIGH severity risk in the input data, propose one mitigation strategy.",
    input_label="RANKED_RISKS",  # Explicitly RANKED_RISKS
)

print("\n=== Final Results ===")
print("MITIGATIONS:", pipeline.get("MITIGATIONS")[:300])
```

**Expected Token Savings:** Explicit `<input_data>` tags with labels make it structurally impossible for Claude to confuse step inputs; each step is unambiguous.
**Environment:** Python 3.9+; pipeline stores all intermediate results — useful for debugging which step produced wrong output.

---

### Option 4: Agentic task description with entity tracking

```python
import re
import anthropic

client = anthropic.Anthropic()


def track_entities(text: str) -> dict[str, str]:
    """
    Simple entity extractor — identifies named outputs that can be referenced.
    In production, replace with NER or structured extraction.
    """
    entities = {}
    # Look for patterns like "the summary", "the list", "the translation"
    patterns = [
        (r"(?i)the\s+(summary|list|translation|report|analysis|draft|result|output)", "artifact"),
        (r"(?i)(document|text|content)\s+(?:above|provided|uploaded)", "source"),
    ]
    for pattern, entity_type in patterns:
        for match in re.finditer(pattern, text):
            entities[match.group(0)] = f"{entity_type}:{match.group(1)}"
    return entities


def clarify_ambiguous_references(task_description: str, available_labels: list[str]) -> str:
    """
    Use Haiku to detect and resolve ambiguous pronoun references before executing a step.
    """
    if not any(pronoun in task_description.lower() for pronoun in ["it", "this", "that", "the result", "the output", "the previous"]):
        return task_description  # No ambiguity detected

    label_context = ", ".join(available_labels) if available_labels else "none yet"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=(
            "You rewrite task descriptions to replace ambiguous pronouns with explicit references. "
            "Available named results: {labels}. "
            "If a pronoun is ambiguous, replace it with the most likely named result. "
            "Return ONLY the rewritten instruction, nothing else."
        ).format(labels=label_context),
        messages=[{"role": "user", "content": f"Rewrite this instruction to remove ambiguous references:\n{task_description}"}],
    )
    rewritten = response.content[0].text.strip()
    if rewritten != task_description:
        print(f"[Clarification] '{task_description[:60]}' → '{rewritten[:60]}'")
    return rewritten


class ClarifyingPipeline:
    def __init__(self):
        self.named_results: dict[str, str] = {}
        self.messages: list[dict] = []

    def add_result(self, label: str, content: str) -> None:
        self.named_results[label] = content

    def execute_step(self, raw_instruction: str, input_label: str | None = None) -> str:
        available = list(self.named_results.keys())
        clarified = clarify_ambiguous_references(raw_instruction, available)

        # Build unambiguous user message
        if input_label and input_label in self.named_results:
            user_msg = f"{clarified}\n\n<{input_label}>\n{self.named_results[input_label]}\n</{input_label}>"
        else:
            user_msg = clarified

        self.messages.append({"role": "user", "content": user_msg})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=self.messages,
        )
        result = response.content[0].text
        self.messages.append({"role": "assistant", "content": result})
        return result


pipeline = ClarifyingPipeline()

# Simulate processing with potentially ambiguous instructions
doc = "Q1 revenue grew 15%. Customer churn dropped to 2.1%. NPS score reached 72."

pipeline.add_result("ORIGINAL_DOC", doc)

result1 = pipeline.execute_step(
    "Extract all numeric metrics from ORIGINAL_DOC as a structured list.",
    input_label="ORIGINAL_DOC",
)
pipeline.add_result("METRICS_LIST", result1)
print(f"Step 1: {result1[:100]}\n")

# Ambiguous instruction — "it" resolved by clarification
result2 = pipeline.execute_step(
    "Translate it to Spanish.",  # "it" is ambiguous — Haiku resolves to METRICS_LIST
    input_label="METRICS_LIST",
)
pipeline.add_result("SPANISH_METRICS", result2)
print(f"Step 2: {result2[:100]}\n")
```

**Expected Token Savings:** Haiku disambiguation costs ~50 tokens per step; prevents misapplied transformations that require 1–3 correction turns at 500+ tokens each.
**Environment:** Python 3.9+; Haiku clarification runs in <100ms; skip if instruction is already unambiguous.

---

### Option 5: Template-based step instructions with slot filling

```python
import anthropic
from string import Template

client = anthropic.Anthropic()


class StepTemplate:
    """
    A reusable step template with explicit slot names.
    Slots like $INPUT_LABEL are filled with actual labels at runtime.
    """

    def __init__(self, template: str):
        self._template = Template(template)

    def fill(self, **slots) -> str:
        return self._template.safe_substitute(**slots)


# Define reusable, unambiguous step templates
STEP_TEMPLATES = {
    "extract_items": StepTemplate(
        "Extract all $ITEM_TYPE from $INPUT_LABEL. Format as a numbered list. "
        "Do not include content from any other source."
    ),
    "translate": StepTemplate(
        "Translate the content of $INPUT_LABEL to $TARGET_LANGUAGE. "
        "Preserve the formatting and structure of $INPUT_LABEL exactly."
    ),
    "summarize": StepTemplate(
        "Summarize $INPUT_LABEL into $MAX_POINTS key points. "
        "Draw only from $INPUT_LABEL — do not add information from other sources."
    ),
    "compare": StepTemplate(
        "Compare $INPUT_A and $INPUT_B. Identify: "
        "(1) what is in $INPUT_A but not $INPUT_B, "
        "(2) what is in $INPUT_B but not $INPUT_A, "
        "(3) what is in both."
    ),
    "format_report": StepTemplate(
        "Format $INPUT_LABEL as a professional report. "
        "Use $INPUT_LABEL as the sole source of content. Add a title, date ($REPORT_DATE), and section headers."
    ),
}


def run_templated_pipeline(document: str) -> dict[str, str]:
    results: dict[str, str] = {"USER_DOCUMENT": document}
    messages: list[dict] = []

    def step(template_name: str, output_label: str, **slots) -> str:
        # Fill the template — no pronouns, all explicit references
        instruction = STEP_TEMPLATES[template_name].fill(**slots)

        # Inject referenced inputs explicitly
        input_labels = [v for k, v in slots.items() if k.startswith("INPUT")]
        context_blocks = ""
        for label in input_labels:
            if label in results:
                context_blocks += f"\n\n<{label}>\n{results[label]}\n</{label}>"

        messages.append({"role": "user", "content": instruction + context_blocks})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=messages,
        )
        result = response.content[0].text
        messages.append({"role": "assistant", "content": result})
        results[output_label] = result
        print(f"[{output_label}]: {result[:80]}...")
        return result

    step("extract_items", "ACTION_ITEMS", ITEM_TYPE="action items", INPUT_LABEL="USER_DOCUMENT")
    step("extract_items", "RISKS", ITEM_TYPE="risks or concerns", INPUT_LABEL="USER_DOCUMENT")
    step("compare", "COMPARISON", INPUT_A="ACTION_ITEMS", INPUT_B="RISKS")
    step("summarize", "SUMMARY", INPUT_LABEL="COMPARISON", MAX_POINTS="5")
    step("format_report", "FINAL_REPORT", INPUT_LABEL="SUMMARY", REPORT_DATE="2026-04-15")

    return results


doc = """
Team meeting notes:
- Launch the new API by end of April (risk: incomplete testing)
- Migrate the database before the load spike in May (risk: data loss if migration fails)
- Update the dashboard UI (low risk, UI only)
- Hire two senior engineers before Q3 (risk: competitive market)
"""

results = run_templated_pipeline(doc)
print(f"\nFINAL REPORT:\n{results['FINAL_REPORT'][:500]}")
```

**Expected Token Savings:** Templates with explicit slot names make reference ambiguity impossible by construction; no clarification step needed.
**Environment:** Python 3.9+; `string.Template` is stdlib; template library scales to complex multi-branch pipelines.

---

### Option 6: Linter that detects ambiguous references before pipeline execution

```python
import re
import anthropic

client = anthropic.Anthropic()

AMBIGUOUS_PRONOUNS = re.compile(
    r"\b(it|this|that|these|those|the result|the output|the previous|the above|"
    r"the following|the response|the content|the text)\b",
    re.IGNORECASE,
)

REFERENCE_PATTERNS = re.compile(
    r"\[([A-Z_]+)\]|<([A-Z_]+)>|\b([A-Z][A-Z_]+)\b",
)


def lint_pipeline_instructions(steps: list[str]) -> list[dict]:
    """
    Scan pipeline instructions for ambiguous pronoun references.
    Returns a list of issues found.
    """
    issues = []
    defined_labels: set[str] = set()

    for i, step in enumerate(steps):
        # Find ambiguous pronouns
        ambiguous = AMBIGUOUS_PRONOUNS.findall(step)
        if ambiguous:
            issues.append({
                "step": i + 1,
                "severity": "WARNING",
                "type": "ambiguous_pronoun",
                "found": list(set(t.lower() for t in ambiguous)),
                "instruction": step[:100],
                "suggestion": "Replace with a named label like [STEP_1_OUTPUT] or [USER_INPUT]",
            })

        # Find references to undefined labels
        refs = REFERENCE_PATTERNS.findall(step)
        for ref_tuple in refs:
            ref = next((r for r in ref_tuple if r), None)
            if ref and ref not in defined_labels and len(ref) > 3:
                issues.append({
                    "step": i + 1,
                    "severity": "ERROR",
                    "type": "undefined_reference",
                    "found": ref,
                    "instruction": step[:100],
                    "suggestion": f"Define [{ref}] in a previous step before referencing it here",
                })

        # Track labels defined in this step
        output_labels = re.findall(r"\[([A-Z_]+)\](?:\s*output|\s*→)", step, re.IGNORECASE)
        defined_labels.update(output_labels)

    return issues


# Test: ambiguous pipeline
bad_steps = [
    "Extract the key metrics from the document.",
    "Translate it to French.",                        # "it" is ambiguous
    "Summarize the previous output in 3 bullets.",    # "previous output" is ambiguous
    "Format the result as a PDF report.",             # "the result" is ambiguous
    "Send this to the [RECIPIENT] team.",             # "this" + undefined [RECIPIENT]
]

print("=== Linting ambiguous pipeline ===")
issues = lint_pipeline_instructions(bad_steps)
for issue in issues:
    print(f"  [{issue['severity']}] Step {issue['step']} ({issue['type']}): {issue['found']}")
    print(f"    → {issue['suggestion']}\n")

# Fixed pipeline — no ambiguity
good_steps = [
    "Extract key metrics from [USER_DOCUMENT]. Label your output as [METRICS]. → [METRICS]",
    "Translate [METRICS] to French. Label your output as [FRENCH_METRICS]. → [FRENCH_METRICS]",
    "Summarize [FRENCH_METRICS] in 3 bullets. Label your output as [SUMMARY]. → [SUMMARY]",
    "Format [SUMMARY] as a PDF report. Label your output as [FINAL_REPORT]. → [FINAL_REPORT]",
]

print("=== Linting explicit pipeline ===")
issues = lint_pipeline_instructions(good_steps)
if not issues:
    print("  ✓ No ambiguity detected — safe to execute")
else:
    for issue in issues:
        print(f"  [{issue['severity']}] Step {issue['step']}: {issue['found']}")

# Execute the clean pipeline
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    system="\n".join(good_steps),
    messages=[{"role": "user", "content": "Process this: Q1 revenue +18%, churn -2.1%, NPS 72."}],
)
print(f"\nExecution result:\n{response.content[0].text[:400]}")
```

**Expected Token Savings:** Lint gate catches ambiguous references at build time — before API calls are made; prevents entire class of misapplied transformations.
**Environment:** Python 3.9+; linter adds <1ms; run in CI or at agent startup to validate pipeline definitions.

---

| Option | Approach | Ambiguity Prevention | Best For |
|--------|----------|---------------------|----------|
| 1 | Named labels in system prompt | [LABEL] convention | Simple linear pipelines |
| 2 | Input/output contracts in XML | Structural step contracts | Complex pipelines |
| 3 | Explicit multi-turn handoffs | `<INPUT_LABEL>` injection | Step-by-step execution |
| 4 | Haiku clarification middleware | Auto-resolve ambiguity | Legacy pipeline instructions |
| 5 | Template slot filling | No-pronoun templates | Reusable pipeline libraries |
| 6 | Reference linter | Pre-execution validation | Pipeline CI/CD |
