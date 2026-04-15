---
layout: solution
title: "Agent Fails at Multi-Step Reasoning — Skips Intermediate Steps"
category: prompt-engineering
description: "An agent is given a problem requiring several reasoning steps to solve correctly. Instead of working through each step, it jumps to a conclusion — often plausible-sounding but wrong. It skips verification steps, assumes intermediate results without computing them, or conflates multiple steps into one. The fix is to structure the prompt to elicit step-by-step reasoning and verify intermediate results before proceeding."
tags: [prompt-engineering, reasoning, chain-of-thought, multi-step, verification, accuracy, structured-thinking]
---

# Agent Fails at Multi-Step Reasoning — Skips Intermediate Steps

## Symptom
- Agent gives a confident wrong answer to a math problem it could solve if it showed its work
- Multi-step business logic produces wrong output because agent assumed a sub-result instead of computing it
- Agent skips eligibility checks, jumps to "yes" or "no" without verifying each condition
- Complex data transformation produces wrong result — intermediate representation was skipped
- Agent summarizes research correctly but draws wrong conclusions by not reasoning through implications
- Agent solves "step 1" and "step 3" but invents the result of "step 2"

## Root Cause
LLMs have a strong prior toward producing a fluent, complete-looking answer quickly. For multi-step problems, this means the model often leaps to the final answer, skipping intermediate computation. Without explicit structure forcing each step, the model may fill in intermediate results with plausible-but-unchecked values. The fix is to use structured prompts, tool-based grounding, or explicit step enumeration to force visible intermediate work.

## Fix

### Option 1: Explicit step enumeration in the system prompt

```python
import anthropic

client = anthropic.Anthropic()

# WEAK — open-ended question invites step-skipping:
WEAK_SYSTEM = "You are a helpful assistant. Answer questions accurately."

# STRONG — step-by-step structure is mandatory:
STRONG_SYSTEM = """You are a careful reasoning assistant.

For any problem requiring multiple steps, you MUST:
1. Identify and list each required step BEFORE starting
2. Work through each step explicitly, showing your work
3. State the result of each step as a fact before proceeding to the next
4. Only draw conclusions after all steps are complete

Format:
## Steps Required
- Step 1: [what you need to do]
- Step 2: [what you need to do]
...

## Working Through Steps

**Step 1:** [description]
Work: [computation or reasoning]
Result: [explicit result]

**Step 2:** [description]
Work: [computation using Step 1 result]
Result: [explicit result]

...

## Final Answer
[conclusion drawn from the step results above]
"""


def careful_reasoning(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=STRONG_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text


# Test:
q = """A store sells widgets at $12.50 each. If a customer buys 7 widgets and has a
20% discount coupon, and sales tax is 8.5%, what is the final price?"""

answer = careful_reasoning(q)
print(answer)
# Agent is forced to show:
# Step 1: Base price (7 × $12.50 = $87.50)
# Step 2: Apply discount ($87.50 × 0.80 = $70.00)
# Step 3: Apply tax ($70.00 × 1.085 = $75.95)
# Final: $75.95
```

### Option 2: Tool-grounded intermediate steps — compute, don't guess

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

# Force intermediate steps to go through tools so results are computed,
# not assumed. The model cannot fabricate tool outputs.

COMPUTE_TOOL = {
    "name": "calculate",
    "description": "Perform an arithmetic calculation. Use this for EVERY numerical computation — never do mental math.",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Python arithmetic expression to evaluate (e.g., '87.50 * 0.80')"
            },
            "step_description": {
                "type": "string",
                "description": "What this calculation represents (e.g., 'applying 20% discount')"
            }
        },
        "required": ["expression", "step_description"]
    }
}

LOOKUP_TOOL = {
    "name": "lookup_fact",
    "description": "Look up a fact before using it in reasoning. Use for any value you need to verify.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fact_key": {
                "type": "string",
                "description": "The fact to look up (e.g., 'sales_tax_rate', 'discount_rate')"
            }
        },
        "required": ["fact_key"]
    }
}

VERIFY_TOOL = {
    "name": "verify_step",
    "description": "Record and verify a reasoning step result before using it in the next step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "step_number": {"type": "integer"},
            "step_description": {"type": "string"},
            "result": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": ["certain", "likely", "uncertain"],
                "description": "How confident you are in this step result"
            }
        },
        "required": ["step_number", "step_description", "result", "confidence"]
    }
}

SYSTEM = """You are a careful reasoning assistant.
For multi-step problems:
1. Use the `calculate` tool for EVERY arithmetic operation (never compute mentally)
2. Use `verify_step` to record each intermediate result before proceeding
3. Only write your final answer after all steps are verified
4. If any step is 'uncertain', say so explicitly."""


def evaluate_expression(expr: str) -> float | str:
    """Safely evaluate arithmetic expressions."""
    # Allow only safe math characters:
    if not re.match(r'^[\d\s\+\-\*\/\(\)\.\%]+$', expr.replace('**', '^')):
        return "Error: expression contains invalid characters"
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return round(float(result), 2)
    except Exception as e:
        return f"Error: {e}"


FACT_DB = {
    "sales_tax_rate": "8.5% (0.085)",
    "standard_discount": "20% (0.20)",
    "widget_price": "$12.50",
    "min_order_for_free_shipping": "$50.00"
}


def handle_tool(name: str, inputs: dict) -> str:
    if name == "calculate":
        result = evaluate_expression(inputs["expression"])
        return json.dumps({
            "step": inputs.get("step_description", ""),
            "expression": inputs["expression"],
            "result": result
        })

    elif name == "lookup_fact":
        key = inputs["fact_key"].lower()
        value = FACT_DB.get(key, f"Fact '{key}' not found in database")
        return json.dumps({"fact": key, "value": value})

    elif name == "verify_step":
        confidence_emoji = {"certain": "✓", "likely": "~", "uncertain": "?"}
        marker = confidence_emoji.get(inputs.get("confidence", "certain"), "✓")
        return json.dumps({
            "recorded": True,
            "step": inputs["step_number"],
            "description": inputs["step_description"],
            "result": inputs["result"],
            "status": f"{marker} Step {inputs['step_number']} verified"
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


def grounded_reasoning(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    tools = [COMPUTE_TOOL, LOOKUP_TOOL, VERIFY_TOOL]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


answer = grounded_reasoning(
    "7 widgets at $12.50 each, 20% discount, 8.5% tax. What's the total?"
)
print(answer)
```

### Option 3: Pre-filled assistant turn — prime the step-by-step format

```python
import anthropic

client = anthropic.Anthropic()

# Technique: inject the beginning of the assistant's response
# to lock it into a step-by-step format before it can skip steps.

def force_step_by_step(question: str, domain: str = "general") -> str:
    """
    Pre-fill the assistant's first turn to enforce step-by-step reasoning.
    The model must continue from the primed format.
    """
    PRIMER = "Let me work through this step by step.\n\n**Step 1:**"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": question},
            # Pre-filled assistant turn forces the model into a reasoning pattern:
            {"role": "assistant", "content": PRIMER}
        ]
    )
    # The response continues from PRIMER:
    return PRIMER + response.content[0].text


# Eligibility check example:
question = """
A customer wants to apply for our premium credit card. They must meet ALL of these criteria:
- Credit score ≥ 720
- Annual income ≥ $75,000
- No bankruptcies in the last 7 years
- Account age ≥ 2 years

Customer profile:
- Credit score: 745
- Annual income: $68,000
- Last bankruptcy: 2019 (5 years ago)
- Account age: 3 years

Is this customer eligible?
"""

result = force_step_by_step(question)
print(result)
# Forces the model to check each criterion explicitly rather than
# jumping to a conclusion about "close enough"
```

### Option 4: Chain-of-thought verification — second pass checks the reasoning

```python
import anthropic

client = anthropic.Anthropic()

def reason_then_verify(question: str) -> dict:
    """
    Two-pass approach:
    1. Generate reasoning chain
    2. Verify each step in the chain is correct
    """
    # Pass 1: Generate step-by-step reasoning:
    reasoning_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Work through this problem step by step. Show all intermediate results.",
        messages=[{"role": "user", "content": question}]
    )
    reasoning = reasoning_response.content[0].text

    # Pass 2: Verify the reasoning chain:
    verification_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Review this reasoning chain and check each step:

Question: {question}

Reasoning:
{reasoning}

For each numbered step:
1. Is the step logically sound?
2. Is the arithmetic correct (if applicable)?
3. Does it correctly use the result from the previous step?

List any errors found, or confirm "All steps verified" if correct.
Then state the correct final answer."""
        }]
    )
    verification = verification_response.content[0].text

    # Determine if verification found errors:
    has_errors = "error" in verification.lower() or "incorrect" in verification.lower() or "wrong" in verification.lower()

    return {
        "initial_reasoning": reasoning,
        "verification": verification,
        "errors_found": has_errors,
        "final_answer": verification  # verification contains the corrected answer
    }


result = reason_then_verify(
    "If 40% of a group are women, and 30% of the women are managers, "
    "and there are 200 people in the group, how many women are managers?"
)
print("Initial reasoning:")
print(result["initial_reasoning"])
print("\nVerification:")
print(result["verification"])
```

### Option 5: Decomposition tool — force the model to plan steps before executing

```python
import anthropic
import json

client = anthropic.Anthropic()

DECOMPOSE_TOOL = {
    "name": "decompose_problem",
    "description": "FIRST TOOL TO CALL: Decompose a complex problem into ordered steps before solving it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "problem_statement": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_number": {"type": "integer"},
                        "description": {"type": "string"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Step numbers this step depends on"
                        }
                    },
                    "required": ["step_number", "description"]
                },
                "description": "Ordered list of steps to solve the problem"
            }
        },
        "required": ["problem_statement", "steps"]
    }
}

SOLVE_STEP_TOOL = {
    "name": "solve_step",
    "description": "Solve one step of the decomposed plan. Call after decompose_problem.",
    "input_schema": {
        "type": "object",
        "properties": {
            "step_number": {"type": "integer"},
            "computation": {"type": "string", "description": "Show your work"},
            "result": {"type": "string", "description": "The result of this step"}
        },
        "required": ["step_number", "computation", "result"]
    }
}

SYSTEM = """You are a careful problem solver.
For any complex question:
1. ALWAYS call decompose_problem FIRST to plan your approach
2. Then call solve_step for EACH step in your plan
3. Only give your final answer after all steps are solved
Never skip steps or solve without decomposing first."""


def structured_problem_solver(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    tools = [DECOMPOSE_TOOL, SOLVE_STEP_TOOL]
    step_results = {}
    plan = None

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "decompose_problem":
                plan = block.input["steps"]
                print(f"\n[Plan: {len(plan)} steps]")
                for s in plan:
                    print(f"  Step {s['step_number']}: {s['description']}")
                result = json.dumps({"plan_recorded": True, "step_count": len(plan)})

            elif block.name == "solve_step":
                num = block.input["step_number"]
                step_results[num] = block.input["result"]
                print(f"\n[Step {num} solved]")
                print(f"  Work: {block.input['computation']}")
                print(f"  Result: {block.input['result']}")
                result = json.dumps({"step": num, "result": block.input["result"], "recorded": True})

            else:
                result = json.dumps({"error": f"Unknown tool: {block.name}"})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


answer = structured_problem_solver(
    "A train travels from City A to City B at 80 mph. The return trip is at 60 mph. "
    "The one-way distance is 240 miles. What is the average speed for the entire round trip?"
)
print("\nFinal answer:", answer)
```

### Option 6: Self-consistency sampling — vote across multiple reasoning chains

```python
import anthropic
import asyncio
from collections import Counter

client = anthropic.AsyncAnthropic()

async def single_reasoning_chain(question: str, seed_phrase: str = "") -> str:
    """Generate one reasoning chain."""
    content = f"{seed_phrase}\n\n{question}" if seed_phrase else question
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Solve step by step. Show all intermediate calculations. End with 'FINAL ANSWER: [value]'.",
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text


def extract_final_answer(text: str) -> str | None:
    """Extract the final answer from a reasoning chain."""
    import re
    match = re.search(r'FINAL ANSWER:\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
    return match.group(1).strip() if match else None


async def self_consistency_solve(question: str, n_samples: int = 5) -> dict:
    """
    Generate N independent reasoning chains and vote on the most common answer.
    Reduces single-chain errors from step-skipping or mistakes.
    """
    # Vary the seed to get diverse reasoning chains:
    seeds = [
        "Let me work through this carefully.",
        "I'll solve this step by step.",
        "Breaking this down methodically:",
        "Let me compute each step:",
        "Working through this problem:"
    ]

    chains = await asyncio.gather(*[
        single_reasoning_chain(question, seeds[i % len(seeds)])
        for i in range(n_samples)
    ])

    answers = [extract_final_answer(chain) for chain in chains]
    valid_answers = [a for a in answers if a]

    if not valid_answers:
        return {"error": "No valid answers extracted", "chains": chains}

    # Vote:
    vote = Counter(valid_answers)
    winning_answer, count = vote.most_common(1)[0]
    confidence = count / n_samples

    print(f"[self-consistency] Answers: {dict(vote)}")
    print(f"[self-consistency] Winner: {winning_answer!r} ({confidence:.0%} agreement)")

    # Find the chain that produced the winning answer for display:
    best_chain = next(
        (c for c, a in zip(chains, answers) if a == winning_answer),
        chains[0]
    )

    return {
        "answer": winning_answer,
        "confidence": confidence,
        "vote_distribution": dict(vote),
        "best_chain": best_chain
    }


result = asyncio.run(self_consistency_solve(
    "What is the average speed of a round trip where the outbound leg is 240 miles at 80 mph "
    "and the return leg is 240 miles at 60 mph?",
    n_samples=5
))
print(f"Final: {result['answer']} (confidence: {result['confidence']:.0%})")
```

## Step-Skipping Failure Modes and Fixes

| Failure Mode | Root Cause | Fix |
|---|---|---|
| Arithmetic shortcut | Estimates instead of computing | Use `calculate` tool for all math (Option 2) |
| Assumed intermediate | Skips sub-calculation | Explicit step enumeration in prompt (Option 1) |
| Merged steps | Conflates two reasoning steps | `decompose_problem` tool forces planning (Option 5) |
| Wrong step order | Out-of-order reasoning | Dependency tracking in decomposition (Option 5) |
| Plausible but wrong conclusion | Skipped verification | Two-pass verify (Option 4) |
| One wrong chain | Single-sample variance | Self-consistency voting (Option 6) |

## Expected Token Savings
Self-consistency (Option 6) uses N× tokens but dramatically reduces error rate. Tool-based computation (Option 2) adds 2–4 tool-call overhead but prevents costly reasoning errors that require re-runs. For high-stakes multi-step reasoning, the verification cost is always less than the cost of acting on a wrong answer.

## Environment
- Any agent solving problems with more than 2 dependent steps; critical for financial calculations, legal eligibility checks, data transformations, and compliance verification; the step enumeration system prompt (Option 1) is zero-cost and should be applied to all reasoning tasks; tool-grounded computation (Option 2) is the most reliable fix for arithmetic-heavy problems; self-consistency (Option 6) is best for high-stakes questions where correctness is critical
