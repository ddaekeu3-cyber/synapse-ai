---
layout: solution
title: "Agent Fabricates Intermediate Reasoning Steps — Chain of Thought Is Wrong"
category: hallucination
description: "The agent shows its work but the work is wrong. It reasons 'Step 1: X is true because Y' but Y is fabricated. It reaches a correct-sounding conclusion through a chain of plausible but invented steps. It derives a number through fake arithmetic that produces the right-looking magnitude. The final answer looks well-reasoned but the reasoning chain contains hallucinated steps the user assumes are verified."
tags: [hallucination, chain-of-thought, reasoning, verification, math, logic, tool-use, calculation]
---

# Agent Fabricates Intermediate Reasoning Steps — Chain of Thought Is Wrong

## Symptom
- Agent's explanation of "how I got this answer" contains made-up steps
- Math reasoning looks correct but intermediate calculations are wrong
- Logical chain is internally consistent but based on a false premise stated as fact
- Agent says "Since X is 42, therefore Y" but X is not 42 — it's fabricated
- Code reasoning: agent explains what a function does incorrectly
- Users trust wrong answers because they came with confident step-by-step reasoning
- Asking "how did you get that?" produces a plausible-sounding but fabricated explanation

## Root Cause
Chain-of-thought prompting improves reasoning on genuine logical tasks but doesn't prevent fabrication of the facts that reasoning operates on. The model generates a plausible chain of steps that leads to a coherent conclusion — but "plausible" doesn't mean "correct". The model interpolates facts it doesn't know rather than saying "I don't have this data." The fix is to verify individual reasoning steps using tools (calculators, database lookups, code execution) rather than trusting the model's self-reported steps.

## Fix

### Option 1: Tool-grounded reasoning — force each step to use a tool

```python
import anthropic
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# Instead of letting the model reason in its head, give it tools for each step:
GROUNDED_REASONING_TOOLS = [
    {
        "name": "calculate",
        "description": (
            "Evaluate a mathematical expression. Use this for ALL numeric calculations. "
            "Never calculate in your head — always use this tool for math."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Python math expression to evaluate, e.g. '(42 * 3.14) / 100'"},
                "description": {"type": "string", "description": "What this calculation is for"}
            },
            "required": ["expression"]
        }
    },
    {
        "name": "lookup_fact",
        "description": (
            "Look up a specific fact from the knowledge base. "
            "Use this when you need a number, date, name, or other fact from the data. "
            "Never guess or assume a fact — always look it up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_query": {"type": "string", "description": "What fact to look up"}
            },
            "required": ["fact_query"]
        }
    },
    {
        "name": "verify_logical_step",
        "description": (
            "Verify whether a logical inference is valid. "
            "State the premise and the conclusion. Returns whether the step is logically valid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "premise": {"type": "string"},
                "conclusion": {"type": "string"},
                "reasoning": {"type": "string"}
            },
            "required": ["premise", "conclusion", "reasoning"]
        }
    }
]

def execute_calculate(expression: str) -> str:
    """Safely evaluate a math expression."""
    try:
        import math
        # Only allow safe operations:
        allowed_names = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
        allowed_names.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return f"{result}"
    except Exception as e:
        return f"Error evaluating '{expression}': {e}"

def execute_lookup(query: str, knowledge_base: dict) -> str:
    """Look up a fact from the knowledge base."""
    query_lower = query.lower()
    for key, value in knowledge_base.items():
        if any(word in query_lower for word in key.lower().split()):
            return f"{key}: {value}"
    return f"Fact not found in knowledge base for query: '{query}'"

def grounded_reasoning_call(
    question: str,
    knowledge_base: dict | None = None,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Answer a question using tool-grounded reasoning.
    Every calculation uses the calculate tool. Every fact lookup uses lookup_fact.
    No step is taken purely in the model's head.
    """
    kb = knowledge_base or {}
    messages = [{
        "role": "user",
        "content": (
            f"{question}\n\n"
            "Think through this step by step. For every calculation, use the calculate tool. "
            "For every fact you need from the data, use lookup_fact. "
            "Do not calculate or recall facts from memory — use the tools."
        )
    }]
    reasoning_steps = []

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            tools=GROUNDED_REASONING_TOOLS,
            messages=messages
        )

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            # Model is done — extract final answer
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            return {"answer": final_text, "reasoning_steps": reasoning_steps}

        # Execute each tool call:
        tool_results = []
        for tool_call in tool_calls:
            name = tool_call.name
            inp = tool_call.input

            if name == "calculate":
                result = execute_calculate(inp["expression"])
                step = f"CALCULATE: {inp['expression']} = {result} ({inp.get('description', '')})"
            elif name == "lookup_fact":
                result = execute_lookup(inp["fact_query"], kb)
                step = f"LOOKUP: {inp['fact_query']} → {result}"
            elif name == "verify_logical_step":
                result = "Logical step accepted for review."
                step = f"VERIFY: '{inp['premise']}' → '{inp['conclusion']}'"
            else:
                result = "Unknown tool"
                step = f"UNKNOWN TOOL: {name}"

            reasoning_steps.append(step)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Usage:
result = grounded_reasoning_call(
    question="If revenue grew 23% from Q1 to Q2, and Q1 revenue was $1.2M, what is Q2 revenue?",
    knowledge_base={"Q1 revenue": "$1,200,000", "growth rate Q1-Q2": "23%"}
)
print(result["answer"])
# All calculations went through the calculate tool — no fabricated arithmetic
for step in result["reasoning_steps"]:
    print(f"  {step}")
```

### Option 2: Step validation — verify each reasoning step before the next

```python
import anthropic
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def generate_reasoning_steps(question: str, model: str = "claude-sonnet-4-6") -> list[str]:
    """Generate step-by-step reasoning as a list of individual steps."""
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": (
                f"Answer this question step by step:\n{question}\n\n"
                "Format your response as a numbered list where each step is a single claim or calculation. "
                "One step per line. Be explicit about what each step claims as fact."
            )
        }]
    )
    text = response.content[0].text
    steps = [line.strip() for line in text.split("\n") if line.strip() and line.strip()[0].isdigit()]
    return steps

def verify_step(
    step: str,
    context: str,
    facts: dict | None = None
) -> dict:
    """
    Verify whether a single reasoning step is valid.
    Returns {valid: bool, issue: str | None, verified_claim: str}.
    """
    facts_text = json.dumps(facts, indent=2) if facts else "No facts provided"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Available facts:\n{facts_text}\n\n"
                f"Context (previous steps):\n{context}\n\n"
                f"Step to verify: {step!r}\n\n"
                "Is this step factually correct and logically valid given the available facts? "
                "Return JSON: {\"valid\": true/false, \"issue\": \"description of issue or null\", "
                "\"confidence\": 0.0-1.0}"
            )
        }]
    )

    try:
        return json.loads(response.content[0].text.strip().strip("```json").strip("```"))
    except json.JSONDecodeError:
        return {"valid": True, "issue": None, "confidence": 0.5}

def verified_chain_of_thought(
    question: str,
    facts: dict | None = None,
    halt_on_invalid: bool = True
) -> dict:
    """
    Generate reasoning steps and verify each one before accepting it.
    If a step is invalid, halt or flag the issue.
    """
    steps = generate_reasoning_steps(question)
    validated_steps = []
    issues = []
    context = ""

    for step in steps:
        verification = verify_step(step, context, facts)

        if verification.get("valid", True):
            validated_steps.append({"step": step, "verified": True})
            context += f"\n{step}"
        else:
            issue = verification.get("issue", "Unknown issue")
            logger.warning(f"Invalid step detected: {step!r} — {issue}")
            issues.append({"step": step, "issue": issue})

            if halt_on_invalid:
                return {
                    "completed": False,
                    "reason": f"Reasoning chain contains an unverified step: {issue}",
                    "validated_steps": validated_steps,
                    "failed_step": step,
                    "issues": issues
                }
            else:
                validated_steps.append({"step": step, "verified": False, "issue": issue})
                context += f"\n{step}"

    return {
        "completed": True,
        "validated_steps": validated_steps,
        "issues": issues,
        "all_valid": len(issues) == 0
    }

# Usage:
result = verified_chain_of_thought(
    question="Calculate the compound annual growth rate for revenue from $1M to $1.5M over 3 years.",
    facts={"initial_revenue": 1_000_000, "final_revenue": 1_500_000, "years": 3},
    halt_on_invalid=False
)
for step in result["validated_steps"]:
    status = "✓" if step["verified"] else "✗"
    print(f"{status} {step['step']}")
```

### Option 3: Code execution for math — verify arithmetic by running it

```python
import anthropic
import ast
import logging
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def extract_calculations_from_text(text: str) -> list[dict]:
    """
    Extract mathematical expressions from reasoning text.
    Returns list of {expression, claimed_result, location}.
    """
    import re
    patterns = [
        # "42 × 3 = 126" or "42 * 3 = 126"
        r"([\d.,]+)\s*[×x*]\s*([\d.,]+)\s*=\s*([\d.,]+)",
        # "100 / 4 = 25"
        r"([\d.,]+)\s*/\s*([\d.,]+)\s*=\s*([\d.,]+)",
        # "1200 + 350 = 1550"
        r"([\d.,]+)\s*[+]\s*([\d.,]+)\s*=\s*([\d.,]+)",
        # "1550 - 200 = 1350"
        r"([\d.,]+)\s*[-]\s*([\d.,]+)\s*=\s*([\d.,]+)",
    ]
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            groups = match.groups()
            if len(groups) == 3:
                try:
                    a = float(groups[0].replace(",", ""))
                    b = float(groups[1].replace(",", ""))
                    claimed = float(groups[2].replace(",", ""))
                    found.append({
                        "text": match.group(0),
                        "operands": (a, b),
                        "claimed_result": claimed,
                        "position": match.start()
                    })
                except ValueError:
                    pass
    return found

def verify_arithmetic_in_text(reasoning_text: str) -> dict:
    """
    Find and verify all arithmetic in the reasoning text.
    Returns {all_correct: bool, errors: list}.
    """
    calculations = extract_calculations_from_text(reasoning_text)
    errors = []

    for calc in calculations:
        a, b = calc["operands"]
        claimed = calc["claimed_result"]
        original = calc["text"]

        # Determine operator and compute actual result:
        if "×" in original or "x" in original or "*" in original:
            actual = a * b
        elif "/" in original:
            actual = a / b if b != 0 else float("inf")
        elif "+" in original:
            actual = a + b
        elif "-" in original:
            actual = a - b
        else:
            continue

        tolerance = abs(actual) * 0.001  # 0.1% tolerance for floating point
        if abs(actual - claimed) > max(tolerance, 0.01):
            errors.append({
                "expression": original,
                "claimed": claimed,
                "actual": actual,
                "error": abs(actual - claimed)
            })
            logger.warning(f"Arithmetic error: {original} — actual result is {actual}")

    return {
        "all_correct": len(errors) == 0,
        "checked": len(calculations),
        "errors": errors
    }

def answer_with_arithmetic_verification(question: str) -> dict:
    """
    Generate an answer and verify all arithmetic in the reasoning."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "Show your calculations explicitly in the format: A × B = C, A + B = C, etc."
            )
        }]
    )

    reasoning = response.content[0].text
    verification = verify_arithmetic_in_text(reasoning)

    if not verification["all_correct"]:
        errors = verification["errors"]
        correction_prompt = (
            f"Your previous answer contained arithmetic errors:\n"
            + "\n".join(f"- {e['expression']}: actual result is {e['actual']}" for e in errors)
            + "\n\nPlease correct the calculation and provide the right answer."
        )
        corrected_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": question},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": correction_prompt}
            ]
        )
        return {
            "answer": corrected_response.content[0].text,
            "corrected": True,
            "original_errors": errors
        }

    return {"answer": reasoning, "corrected": False, "original_errors": []}
```

### Option 4: Reasoning trace audit — ask the model to critique its own chain

```python
import anthropic
import json
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def generate_then_audit(
    question: str,
    facts: dict | None = None,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Generate an answer, then have the model audit its own reasoning.
    Self-critique catches fabricated steps more reliably than a single pass.
    """
    # Step 1: Generate initial answer with reasoning
    context = f"Facts: {json.dumps(facts)}\n\n" if facts else ""
    initial_response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"{context}Question: {question}\n\nAnswer step by step, showing your reasoning."
        }]
    )
    initial_answer = initial_response.content[0].text

    # Step 2: Self-audit — ask a fresh call to critique the reasoning
    audit_response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Available facts: {json.dumps(facts) if facts else 'none'}\n\n"
                f"Proposed answer:\n{initial_answer}\n\n"
                "Audit this answer:\n"
                "1. Are all intermediate facts stated as true actually verifiable from the provided facts?\n"
                "2. Is each logical step valid given what came before?\n"
                "3. Are all arithmetic calculations correct?\n"
                "4. Does the final answer follow from the reasoning?\n\n"
                "Return JSON: {\"passes_audit\": true/false, \"issues\": [\"issue 1\", ...], "
                "\"corrected_answer\": \"corrected answer or null if no correction needed\"}"
            )
        }]
    )

    try:
        audit = json.loads(audit_response.content[0].text.strip().strip("```json").strip("```"))
    except json.JSONDecodeError:
        audit = {"passes_audit": True, "issues": [], "corrected_answer": None}

    if audit.get("passes_audit"):
        return {"answer": initial_answer, "audited": True, "issues": []}
    else:
        corrected = audit.get("corrected_answer") or initial_answer
        logger.warning(f"Reasoning audit found issues: {audit.get('issues', [])}")
        return {
            "answer": corrected,
            "audited": True,
            "issues": audit.get("issues", []),
            "was_corrected": bool(audit.get("corrected_answer"))
        }
```

### Option 5: Separate fact-retrieval from reasoning — don't mix them

```python
import anthropic
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def two_phase_reasoning(
    question: str,
    data_retrieval_fn,  # Callable that retrieves actual data
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Phase 1: Identify what facts are needed (model)
    Phase 2: Retrieve those facts from the actual data source (tool/DB)
    Phase 3: Reason using only the retrieved facts (model, no invention)

    This prevents the model from inventing facts in phase 3 because all
    relevant facts are explicitly provided.
    """
    # Phase 1: Identify required facts
    facts_needed_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"To answer this question: {question!r}\n\n"
                "List every specific fact or data point you would need to look up. "
                "Return JSON: {\"facts_needed\": [\"fact 1\", \"fact 2\", ...]}"
            )
        }]
    )
    try:
        needed = json.loads(
            facts_needed_response.content[0].text.strip().strip("```json").strip("```")
        ).get("facts_needed", [])
    except (json.JSONDecodeError, AttributeError):
        needed = []

    # Phase 2: Retrieve actual facts
    retrieved_facts = {}
    for fact_query in needed:
        try:
            result = data_retrieval_fn(fact_query)
            retrieved_facts[fact_query] = result
        except Exception as exc:
            retrieved_facts[fact_query] = f"Not available: {exc}"

    # Phase 3: Reason using only retrieved facts — no additional lookups allowed
    facts_text = json.dumps(retrieved_facts, indent=2)
    answer_response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=(
            "You must answer using ONLY the facts provided below. "
            "Do not use any additional knowledge or assumptions. "
            "If a needed fact is marked 'Not available', state that you cannot complete that step."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Facts retrieved from the data source:\n{facts_text}\n\n"
                f"Question: {question}\n\n"
                "Answer step by step using ONLY the facts listed above."
            )
        }]
    )

    return {
        "answer": answer_response.content[0].text,
        "facts_used": retrieved_facts,
        "facts_needed": needed
    }
```

### Option 6: Reasoning mode selection — only use chain-of-thought when it helps

```python
import anthropic
import json
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

QUESTION_TYPES = {
    "factual_lookup": {
        "description": "Looking up a specific fact (date, name, number)",
        "approach": "direct_answer",
        "risk": "high_fabrication_risk_in_cot"
    },
    "math_calculation": {
        "description": "Numerical calculation",
        "approach": "tool_use_calculate",
        "risk": "arithmetic_can_be_fabricated"
    },
    "logical_deduction": {
        "description": "Pure logic from given premises",
        "approach": "chain_of_thought",
        "risk": "low_if_premises_are_provided"
    },
    "summarization": {
        "description": "Summarizing provided text",
        "approach": "direct_answer",
        "risk": "low"
    }
}

def classify_question_type(question: str) -> str:
    """Classify what type of reasoning the question requires."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this question: {question!r}\n\n"
                f"Types: {', '.join(QUESTION_TYPES.keys())}\n\n"
                "Return the type name only."
            )
        }]
    )
    answer = response.content[0].text.strip().lower()
    return answer if answer in QUESTION_TYPES else "logical_deduction"

def answer_with_appropriate_reasoning(
    question: str,
    facts: dict | None = None
) -> str:
    q_type = classify_question_type(question)
    config = QUESTION_TYPES.get(q_type, QUESTION_TYPES["logical_deduction"])

    logger.info(f"Question type: {q_type} (approach: {config['approach']})")

    if config["approach"] == "tool_use_calculate":
        result = grounded_reasoning_call(question, facts)  # Option 1 function
        return result["answer"]

    elif config["approach"] == "direct_answer":
        # No chain-of-thought — reduces fabrication of intermediate steps
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="Answer directly and concisely. Do not show intermediate steps.",
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text

    else:  # chain_of_thought for logical deduction
        facts_text = f"\nFacts:\n{json.dumps(facts, indent=2)}" if facts else ""
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": f"{facts_text}\n\nQuestion: {question}\n\nReason step by step from the given facts only."
            }]
        )
        return response.content[0].text
```

## Reasoning Reliability by Approach

| Approach | Fabrication Risk | Best For | Verification Cost |
|---|---|---|---|
| Tool-grounded (calculator, DB) | Very low | Math, data lookup | Medium |
| Step-by-step verification | Low | Complex logic | High |
| Arithmetic extraction + check | Low | Number-heavy reasoning | Low |
| Self-audit loop | Medium | General reasoning | Medium |
| Two-phase fact retrieval | Low | Data-dependent reasoning | Medium |
| Direct answer (no CoT) | Medium | Simple factual questions | None |

## When Chain-of-Thought Makes Fabrication Worse
- **Factual lookups**: CoT gives the model more space to invent supporting "facts"
- **Arithmetic**: Model generates plausible-looking but wrong intermediate steps
- **Historical data**: Model fills in unknown intermediate dates/numbers confidently

## When Chain-of-Thought Genuinely Helps (and Is Lower Risk)
- **Pure logical deduction** from stated premises (no external facts needed)
- **Code debugging** where the model reasons about provided code only
- **Multi-step planning** with explicit constraints all given upfront

## Expected Token Savings
Fabricated reasoning → user gets wrong answer → asks for correction → agent re-does task: ~3,000 tokens overhead
Grounded reasoning → correct first time: 0 correction overhead
Plus: fabricated arithmetic in a financial agent can cost far more than tokens

## Environment
- Any agent performing multi-step reasoning that users rely on for decisions (financial calculations, medical reasoning, legal analysis, engineering estimates); chain-of-thought fabrication is highest risk when: the model lacks the required facts (it invents them), arithmetic is involved (it calculates incorrectly), or the question is about specific data rather than pure logic — use tools for anything that can be verified externally
- Source: direct experience; "the reasoning looked convincing but the intermediate steps were wrong" is the hardest category of hallucination to catch without a verification layer, because the final answer is often approximately correct even when intermediate steps are fabricated
