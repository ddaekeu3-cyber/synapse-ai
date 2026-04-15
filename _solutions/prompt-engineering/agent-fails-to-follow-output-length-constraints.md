---
layout: solution
title: "Agent Ignores Output Length Instructions — Too Long or Too Short"
category: prompt-engineering
description: "You ask for a one-paragraph summary and get a 2,000-word essay. You ask for a detailed report and get three bullet points. The agent acknowledges the length constraint but doesn't follow it. Or it follows it for the first response, then reverts to its default length in subsequent turns."
tags: [prompt-engineering, output-length, instructions, max-tokens, constraints, verbosity, conciseness, system-prompt]
---

# Agent Ignores Output Length Instructions — Too Long or Too Short

## Symptom
- "Summarize in one sentence" produces a 5-paragraph response
- "Give me a detailed analysis" produces a 3-bullet point response
- Agent says "I'll be brief" then writes 800 words
- Length constraint works for turn 1, but by turn 3 the agent is back to its default length
- Agent adds caveats, disclaimers, and "I hope this helps!" padding that wasn't requested
- Asking for a list of 5 items produces 12 items — or 3
- Code generation returns commented-out alternatives nobody asked for

## Root Cause
The model's default length is calibrated toward thoroughness — it adds context, caveats, and alternatives by default. Length constraints in the user message compete against this trained behavior. Constraints stated in passing ("be brief") are weaker than the model's default. Over a multi-turn conversation, length instructions dilute with each turn. The fix is to set length constraints in the system prompt, use `max_tokens` as a hard cap, and use structural constraints (specific item counts, word count targets) rather than vague adjectives.

## Fix

### Option 1: Structural constraints — count-based instead of adjective-based

```python
import anthropic

client = anthropic.Anthropic()

# WRONG — vague adjectives are ignored or inconsistently applied
BAD_CONSTRAINTS = [
    "be brief",
    "keep it short",
    "don't be too long",
    "give a detailed answer",
    "be comprehensive",
]

# RIGHT — structural constraints the model can verify
def build_length_constrained_prompt(
    task: str,
    constraint_type: str,
    constraint_value: int | str
) -> str:
    """
    Build a prompt with a structural (verifiable) length constraint.
    """
    constraint_phrase = {
        "sentences": f"Respond in exactly {constraint_value} sentence{'s' if constraint_value != 1 else ''}.",
        "words": f"Respond in {constraint_value} words or fewer. Count your words before responding.",
        "paragraphs": f"Respond in exactly {constraint_value} paragraph{'s' if constraint_value != 1 else ''}.",
        "items": f"Provide exactly {constraint_value} items in a numbered list. No more, no fewer.",
        "lines": f"Respond in {constraint_value} lines or fewer.",
        "characters": f"Respond in {constraint_value} characters or fewer."
    }.get(constraint_type, f"Keep response under {constraint_value} {constraint_type}.")

    return f"{task}\n\n{constraint_phrase}"

# Examples:
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,  # Hard cap matches the constraint
    messages=[{
        "role": "user",
        "content": build_length_constrained_prompt(
            "What is machine learning?",
            constraint_type="sentences",
            constraint_value=2
        )
    }]
)

# For lists with exact counts:
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=500,
    messages=[{
        "role": "user",
        "content": build_length_constrained_prompt(
            "What are the benefits of containerization?",
            constraint_type="items",
            constraint_value=5
        )
    }]
)
```

### Option 2: max_tokens as a hard enforcement — set it to match the constraint

```python
import anthropic

client = anthropic.Anthropic()

# Token budget by response type — hard caps prevent verbosity
RESPONSE_TOKEN_BUDGETS = {
    "one_sentence": 60,
    "one_paragraph": 150,
    "brief_answer": 100,
    "standard_answer": 400,
    "detailed_answer": 1000,
    "comprehensive_report": 3000,
    "code_snippet": 500,
    "full_function": 800,
    "full_module": 2500,
    "step_by_step": 600,
    "yes_no": 20,
    "classification": 30,
    "summary_3_bullets": 200,
    "summary_5_bullets": 350,
}

def call_with_length_budget(
    prompt: str,
    response_type: str,
    model: str = "claude-sonnet-4-6",
    system: str = ""
) -> str:
    """
    Call Claude with a max_tokens cap that enforces the expected response type.
    The model cannot exceed this length even if it wants to.
    """
    max_tokens = RESPONSE_TOKEN_BUDGETS.get(response_type, 500)

    # Also add the constraint to the prompt for best results
    length_instructions = {
        "one_sentence": "Answer in one sentence only.",
        "one_paragraph": "Answer in one paragraph (3-5 sentences).",
        "brief_answer": "Be very brief. One to three sentences maximum.",
        "yes_no": "Answer YES or NO, optionally with one short reason.",
        "classification": "State the category only. No explanation.",
        "summary_3_bullets": "Summarize in exactly 3 bullet points.",
        "summary_5_bullets": "Summarize in exactly 5 bullet points.",
    }.get(response_type, "")

    full_prompt = f"{prompt}\n\n{length_instructions}".strip() if length_instructions else prompt

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": full_prompt}]
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text

# Usage — length is enforced by both prompt and max_tokens:
answer = call_with_length_budget(
    "What is the difference between HTTP and HTTPS?",
    response_type="one_paragraph"
)
# Max 150 tokens — cannot produce a 2000-word essay

classification = call_with_length_budget(
    "Is this email spam? 'Congratulations! You won a prize!'",
    response_type="yes_no"
)
# Max 20 tokens — forces terse response
```

### Option 3: System prompt length persona — define verbosity level globally

```python
LENGTH_PERSONAS = {
    "terse": """## Response Style: TERSE
- Default to the shortest response that fully answers the question
- No preamble ("Great question!", "Certainly!", "Of course!")
- No summary at the end ("I hope this helps!", "Let me know if...")
- No caveats unless directly asked
- No alternatives unless directly asked
- Lists: exactly as many items as needed, no filler
- Code: no explanatory comments unless asked""",

    "concise": """## Response Style: CONCISE
- Answer directly without lengthy setup
- One paragraph for simple questions, two for complex
- Skip obvious caveats and generic disclaimers
- Bullet points over prose when listing things
- Code examples: include only relevant parts""",

    "balanced": """## Response Style: BALANCED
- Match response length to question complexity
- Simple factual questions: 1-3 sentences
- Conceptual questions: 2-3 paragraphs
- Technical questions: include an example
- Avoid padding and filler phrases""",

    "detailed": """## Response Style: DETAILED
- Provide comprehensive answers with context
- Include relevant examples for technical topics
- Explain reasoning, not just conclusions
- Acknowledge edge cases and tradeoffs
- Structure with headers for multi-part answers""",
}

def build_system_with_length_persona(
    base_system: str,
    verbosity: str = "balanced"
) -> str:
    persona = LENGTH_PERSONAS.get(verbosity, LENGTH_PERSONAS["balanced"])
    return f"{base_system}\n\n{persona}"

# Usage — verbosity level baked into system prompt:
system = build_system_with_length_persona(
    "You are a technical support assistant.",
    verbosity="terse"
)
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=500,
    system=system,
    messages=[{"role": "user", "content": "How do I restart nginx?"}]
)
# Terse system prompt → direct answer, no padding
```

### Option 4: Length validation — check and retry if constraint violated

```python
import anthropic
import re

client = anthropic.Anthropic()

def count_words(text: str) -> int:
    return len(text.split())

def count_sentences(text: str) -> int:
    return len(re.split(r'[.!?]+', text.strip()))

def count_bullet_items(text: str) -> int:
    return len(re.findall(r'^[-•*]\s|^\d+\.\s', text, re.MULTILINE))

def validate_length(
    text: str,
    constraint_type: str,
    target: int,
    tolerance: float = 0.2  # Allow 20% deviation
) -> tuple[bool, str]:
    """
    Check if response meets length constraint.
    Returns (passes, reason).
    """
    actual = {
        "words": count_words(text),
        "sentences": count_sentences(text),
        "items": count_bullet_items(text),
        "characters": len(text),
        "paragraphs": len([p for p in text.split("\n\n") if p.strip()])
    }.get(constraint_type, len(text.split()))

    lower = int(target * (1 - tolerance))
    upper = int(target * (1 + tolerance))

    if actual < lower:
        return False, f"Too short: {actual} {constraint_type} (target: {target}, minimum: {lower})"
    if actual > upper:
        return False, f"Too long: {actual} {constraint_type} (target: {target}, maximum: {upper})"
    return True, f"OK: {actual} {constraint_type}"

def call_with_length_validation(
    prompt: str,
    constraint_type: str,
    target: int,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 2
) -> str:
    """
    Generate response and retry if length constraint violated.
    """
    token_limit = {
        "words": target * 2,        # ~2 tokens per word
        "sentences": target * 50,   # ~50 tokens per sentence
        "items": target * 30,       # ~30 tokens per item
        "characters": target // 3,  # ~3 chars per token
        "paragraphs": target * 150, # ~150 tokens per paragraph
    }.get(constraint_type, 500)

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model=model,
            max_tokens=min(token_limit, 4096),
            messages=messages
        )
        text = response.content[0].text
        valid, reason = validate_length(text, constraint_type, target)

        if valid:
            return text

        print(f"Attempt {attempt + 1}: Length constraint violated — {reason}")

        if attempt < max_retries:
            # Add correction message
            correction = (
                f"Your response was {reason}. "
                f"Please rewrite it to be exactly {target} {constraint_type}. "
                f"Count carefully before responding."
            )
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": correction})

    return text  # Return best effort after max retries
```

### Option 5: Format-enforced brevity — use output schema to control length

```python
import anthropic
import json

client = anthropic.Anthropic()

def get_structured_brief_response(
    question: str,
    schema: dict,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Use tool_choice to enforce a structured, length-constrained response.
    Schema defines exactly what fields to return — prevents rambling.
    """
    response = client.messages.create(
        model=model,
        max_tokens=500,
        tools=[{
            "name": "respond",
            "description": "Provide the structured response",
            "input_schema": schema
        }],
        tool_choice={"type": "tool", "name": "respond"},
        messages=[{"role": "user", "content": question}]
    )

    for block in response.content:
        if block.type == "tool_use":
            return block.input

    return {}

# Example schemas that enforce brevity:
BRIEF_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Direct answer in 1-2 sentences maximum"
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"]
        }
    },
    "required": ["answer", "confidence"]
}

LIST_SCHEMA_5_ITEMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "Exactly 5 items",
            "items": {"type": "string"},
            "minItems": 5,
            "maxItems": 5
        }
    },
    "required": ["items"]
}

# Structured response prevents verbose rambling:
result = get_structured_brief_response(
    "What are the main benefits of Kubernetes?",
    LIST_SCHEMA_5_ITEMS
)
# Returns exactly 5 items — schema enforces this
```

### Option 6: Anti-padding system prompt — explicitly ban filler phrases

```python
ANTI_PADDING_SYSTEM = """## Prohibited Response Patterns

NEVER use these phrases or patterns:
- "Great question!" / "Certainly!" / "Of course!" / "Sure!"
- "I hope this helps!" / "Let me know if you have questions!"
- "Certainly, I'd be happy to help with that!"
- "Based on my understanding..." / "As an AI language model..."
- "In conclusion, ..." summaries when the answer is already complete
- Offering alternatives that weren't requested
- Listing caveats to simple factual questions
- Apologizing for the length of your response
- Explaining what you're about to do before doing it
- Restating the question before answering it

START immediately with the answer. END when the answer is complete.

Example of WRONG response to "What is 2+2?":
"Great question! Based on mathematical principles, 2+2 equals 4. This is a fundamental arithmetic operation. I hope this helps! Let me know if you have any other questions."

Example of RIGHT response to "What is 2+2?":
"4"
"""

def build_padding_free_system(base_system: str) -> str:
    return f"{base_system}\n\n{ANTI_PADDING_SYSTEM}"

# Usage — system prompt explicitly bans common padding patterns:
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1000,
    system=build_padding_free_system("You are a technical documentation assistant."),
    messages=[{"role": "user", "content": "How do I create a Python virtual environment?"}]
)
```

## Length Control Techniques by Effectiveness

| Technique | Effectiveness | Applies To | Notes |
|---|---|---|---|
| `max_tokens` hard cap | High | Any | Hard limit — model cannot exceed |
| Structural count constraint ("exactly 3 items") | High | Lists, bullet points | Verifiable — model can self-check |
| Length persona in system prompt | Medium-High | Whole session | Persistent across turns |
| Anti-padding prompt | Medium | Verbose responses | Removes filler, not actual content |
| Vague adjectives ("be brief") | Low | Any | Inconsistent, reverts quickly |
| Length validation + retry | High | Critical outputs | Catches violations post-generation |
| Forced schema via tool_choice | Very high | Structured output | Schema enforces field constraints |

## Expected Token Savings
Unbounded verbose responses: average 800 tokens per reply in a coding assistant
Terse system prompt + max_tokens: average 200 tokens per reply — 75% output cost reduction
Over 1,000 calls/day: saves ~600,000 output tokens = significant cost reduction

## Environment
- Any agent where output length matters: customer-facing chatbots (verbosity annoys users), classification agents (long answers waste tokens), code generation agents (extra comments add noise), and cost-sensitive batch-processing agents — length control is the highest-ROI output quality improvement for customer-facing agents
- Source: direct experience; unconstrained verbosity is the top output quality complaint from users of general-purpose AI assistants in the first week of deployment
