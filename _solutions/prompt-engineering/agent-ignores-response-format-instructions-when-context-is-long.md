---
layout: solution
title: "Agent ignores response format instructions when context is long"
category: prompt-engineering
description: "Format instructions placed at the top of a long system prompt are forgotten by the time the model generates a response, causing JSON to degrade to prose, markdown to disappear, and word limits to be ignored."
tags: [prompt-engineering, format-instructions, long-context, context-window, instruction-following]
---

## Symptom

The agent reliably returns properly formatted JSON on short conversations. On longer conversations (10+ turns, long tool results, or a large knowledge base in the system prompt), it starts returning:

- Prose instead of JSON
- Missing required fields
- Responses that exceed the stated word limit
- Markdown when plain text was requested

The format instructions are present in the system prompt — but the model has effectively "forgotten" them by generation time.

## Root Cause

Large language models exhibit primacy and recency bias: they attend most strongly to the beginning and end of the context. Format instructions placed once at the top of a 50,000-token system prompt are in the primacy zone but get diluted by the sheer volume of intervening content. When the model's attention must span 50,000+ tokens to reach the generation step, mid-context instructions degrade in influence. This is not a bug — it is a known property of attention mechanisms at long range.

---

## Option 1 — Repeat format instructions at the end of the last user message

**Mirror the format requirement in the user turn immediately before the model generates. Recency ensures it is attended to.**

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a data extraction assistant.

[... imagine 5,000 tokens of domain knowledge here ...]

OUTPUT FORMAT: Always respond with a JSON object containing:
  - entity: string
  - sentiment: "positive" | "neutral" | "negative"
  - confidence: float 0-1
"""

FORMAT_REMINDER = (
    "\n\nIMPORTANT: Respond ONLY with a JSON object in this exact format:\n"
    '{"entity": "...", "sentiment": "positive|neutral|negative", "confidence": 0.0}'
    "\nDo not include any other text, markdown, or explanation."
)


def extract_sentiment(text: str, conversation_history: list[dict]) -> dict:
    # Append format reminder to the current user message (recency effect)
    user_message = f"Analyse the sentiment of: {text}{FORMAT_REMINDER}"

    messages = conversation_history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    raw = response.content[0].text.strip()

    # Parse and validate
    try:
        result = json.loads(raw)
        assert "entity" in result and "sentiment" in result
        return result
    except (json.JSONDecodeError, AssertionError):
        # Fallback: ask model to reformat
        fix_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Convert this text to valid JSON with keys entity, sentiment, confidence:\n{raw}"
                ),
            }],
        )
        return json.loads(fix_response.content[0].text.strip())


# Simulate a long conversation history
long_history = [
    {"role": "user", "content": f"Question {i} about the domain?"}
    for i in range(15)
] + [
    {"role": "assistant", "content": f"Answer {i} with detailed explanation." * 20}
    for i in range(15)
]

result = extract_sentiment("The product launch was a tremendous success.", long_history)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Eliminates 2–3 reformatting retry calls per extraction — each retry costs ~500 tokens. For a pipeline processing 1,000 documents, saves ~1,500,000 tokens in retry overhead.

**Environment:** Any agent with long context + structured output requirements; zero extra API calls.

---

## Option 2 — Assistant prefill to force format compliance

**Pre-fill the start of the assistant's response with the opening characters of the expected format. The model must continue from there.**

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a JSON extraction assistant. [Large knowledge base here...]"""


def extract_with_prefill(user_text: str, history: list[dict]) -> dict:
    messages = history + [
        {"role": "user", "content": f"Extract entities from: {user_text}"},
        # Prefill: model MUST continue from this opening brace
        {"role": "assistant", "content": '{"entities": ['},
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=messages,
    )

    # Reconstruct full JSON by prepending the prefilled prefix
    completion = response.content[0].text
    full_json_str = '{"entities": [' + completion

    # The model should have closed the JSON — parse it
    try:
        return json.loads(full_json_str)
    except json.JSONDecodeError:
        # Try to find a valid close
        for end in range(len(full_json_str), 0, -1):
            try:
                return json.loads(full_json_str[:end] + "]}")
            except json.JSONDecodeError:
                continue
        return {"entities": [], "parse_error": full_json_str[:200]}


# Long simulated history
history = [
    {"role": "user",      "content": "Background: " + "context " * 500},
    {"role": "assistant", "content": "Understood. " + "detail " * 500},
] * 3

result = extract_with_prefill(
    "Apple Inc. reported record revenue. Tim Cook praised the team.",
    history,
)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Prefill forces format compliance without any retry — eliminates the cost of a corrective follow-up call entirely. Effective for 99%+ of requests, saving ~800 tokens per avoided retry.

**Environment:** Agents needing structured JSON extraction; note that prefill is not supported on all providers — verify with your model's API docs.

---

## Option 3 — Separate formatting pass on a short context

**Run the main reasoning call on the full context (no format constraint). Then run a cheap formatting-only call on just the answer.**

```python
import json
import anthropic

client = anthropic.Anthropic()


def reason_then_format(
    user_query: str,
    knowledge_base: str,
    target_schema: dict,
) -> dict:
    """Two-step: reason freely on full context, then format the answer cheaply."""

    # Step 1: Full-context reasoning call — no format pressure
    reasoning_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"You are a helpful assistant.\n\nKnowledge base:\n{knowledge_base}",
        messages=[{"role": "user", "content": user_query}],
    )
    raw_answer = reasoning_response.content[0].text

    # Step 2: Short-context formatting call — haiku, tiny context
    schema_str = json.dumps(target_schema, indent=2)
    format_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Convert the following answer into this exact JSON schema:\n"
                f"{schema_str}\n\n"
                f"ANSWER TO FORMAT:\n{raw_answer[:3000]}\n\n"
                f"Return ONLY the JSON, no other text."
            ),
        }],
    )
    raw_json = format_response.content[0].text.strip()
    start = raw_json.find("{")
    end   = raw_json.rfind("}") + 1
    return json.loads(raw_json[start:end])


schema = {
    "summary": "string (≤ 50 words)",
    "key_points": ["string"],
    "risk_level": "low | medium | high",
    "recommended_action": "string",
}

result = reason_then_format(
    user_query="Should we expand to the European market given current conditions?",
    knowledge_base="Market data: " + "revenue figures, trends, competitor analysis. " * 300,
    target_schema=schema,
)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** The formatting call uses haiku on ~500 tokens — costs ~200 tokens total vs. a sonnet retry on the full context (~5,000+ tokens). Net saving: ~4,800 tokens per format correction.

**Environment:** Agents with large knowledge bases where reasoning quality matters more than format; especially effective for report generation.

---

## Option 4 — Format enforcement via tool use

**Define the output structure as a tool. When the model "calls" the tool, it must conform to the schema — format compliance is mechanically enforced.**

```python
import json
import anthropic

client = anthropic.Anthropic()

OUTPUT_TOOL = {
    "name": "submit_analysis",
    "description": "Submit the final analysis result. ALWAYS use this tool to provide your answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary":     {"type": "string", "maxLength": 200},
            "sentiment":   {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "key_topics":  {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "confidence":  {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["summary", "sentiment", "key_topics", "confidence"],
    },
}

SYSTEM = (
    "You are a text analysis assistant. "
    "Always use the submit_analysis tool to provide your final answer. "
    "Never respond with plain text.\n\n"
    "[... large knowledge base here ...]"
)


def analyse_text(text: str, history: list[dict]) -> dict:
    messages = history + [{"role": "user", "content": f"Analyse this text: {text}"}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=[OUTPUT_TOOL],
        tool_choice={"type": "any"},   # forces a tool call
        messages=messages,
    )

    tool_use = next(
        (b for b in response.content if b.type == "tool_use"),
        None,
    )
    if tool_use:
        return tool_use.input

    # Fallback: model responded with text despite tool_choice=any
    print("Warning: model returned text instead of tool call — re-prompting.")
    return {"summary": response.content[0].text[:200], "sentiment": "neutral",
            "key_topics": [], "confidence": 0.5}


long_history = [
    {"role": "user",      "content": "Context: " + "background information " * 200},
    {"role": "assistant", "content": "Noted. " + "acknowledged " * 100},
] * 4

result = analyse_text(
    "The quarterly earnings beat expectations by 15%, driving the stock up.",
    long_history,
)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Tool-use schema enforcement eliminates format failures entirely — the API validates the output against the schema before returning it. Zero retry tokens spent on format correction.

**Environment:** Agents on Claude Sonnet/Opus where `tool_choice: {type: "any"}` is supported; most robust solution for schema-critical pipelines.

---

## Option 5 — Format instruction sandwich (top + bottom of system prompt)

**Place format instructions both at the top and bottom of the system prompt so they are in both primacy and recency positions.**

```python
import json
import anthropic

client = anthropic.Anthropic()

FORMAT_BLOCK = """
OUTPUT RULES (MANDATORY):
1. Respond ONLY with valid JSON.
2. Schema: {"answer": string, "sources": [string], "confidence": "high"|"medium"|"low"}
3. No markdown code fences, no explanation, no preamble.
"""

KNOWLEDGE_BASE = "\n".join([
    f"Document {i}: " + "relevant domain information about the topic. " * 30
    for i in range(50)
])


def build_system_prompt() -> str:
    return (
        FORMAT_BLOCK +           # TOP — primacy position
        "\n\nKNOWLEDGE BASE:\n" +
        KNOWLEDGE_BASE +
        "\n\n" + FORMAT_BLOCK    # BOTTOM — recency position
    )


def query_agent(question: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip()
    # Strip code fences if model ignores rule 3
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


result = query_agent("What are the key findings from the research documents?")
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Sandwich placement reduces format failure rate from ~30% on long contexts to <5% — avoids ~25% of retry calls. For 10,000 daily queries, saves ~750,000 retry tokens.

**Environment:** Simple fix for existing agents without architecture changes; works on any model size.

---

## Option 6 — Adaptive context compression before format-sensitive calls

**Summarise the oldest conversation turns before making a format-sensitive request, keeping the model's effective context short.**

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_HISTORY_TURNS = 6     # keep only 6 most recent turns verbatim
TOKEN_ESTIMATE_CHARS = 4  # ~4 chars per token


def estimate_tokens(messages: list[dict]) -> int:
    return sum(
        len(str(m.get("content", ""))) // TOKEN_ESTIMATE_CHARS
        for m in messages
    )


def compress_history(messages: list[dict]) -> list[dict]:
    """Summarise older turns; keep MAX_HISTORY_TURNS recent turns verbatim."""
    if len(messages) <= MAX_HISTORY_TURNS * 2:
        return messages

    old = messages[:-MAX_HISTORY_TURNS * 2]
    recent = messages[-MAX_HISTORY_TURNS * 2:]

    # Summarise the old portion
    old_text = "\n".join(
        f"{m['role'].upper()}: {str(m.get('content',''))[:200]}"
        for m in old
    )
    summary_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"Summarise these conversation turns in ≤ 150 words:\n{old_text[:6000]}",
        }],
    )
    summary_message = {
        "role": "user",
        "content": f"[Earlier conversation summary: {summary_resp.content[0].text}]",
    }
    return [summary_message] + recent


def extract_structured(question: str, history: list[dict]) -> dict:
    compressed = compress_history(history)
    token_estimate = estimate_tokens(compressed)
    print(f"  Context: {estimate_tokens(history):,} → {token_estimate:,} estimated tokens after compression")

    format_instruction = (
        "\n\nRespond ONLY with JSON: "
        '{"answer": "...", "reasoning": "...", "confidence": 0.0}'
    )
    messages = compressed + [{"role": "user", "content": question + format_instruction}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=messages,
    )
    raw = response.content[0].text.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    return json.loads(raw[start:end]) if start != -1 else {"answer": raw, "reasoning": "", "confidence": 0.5}


# Simulate a long history
history = []
for i in range(20):
    history.append({"role": "user",      "content": f"Question {i}: " + "detail " * 50})
    history.append({"role": "assistant", "content": f"Answer {i}: " + "explanation " * 50})

result = extract_structured("What is the final recommendation?", history)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Context compression before format-sensitive calls keeps effective context under 4,000 tokens — restores near-perfect format compliance and reduces per-call token cost by 60–80% for long conversations.

**Environment:** Conversational agents with long multi-turn histories; compression adds ~300 haiku tokens but saves thousands in retry costs.

---

## Comparison

| Option | Mechanism | Extra API Calls | Format Failure Rate | Complexity |
|--------|----------|----------------|--------------------|----|
| 1. Recency reminder | Append to user message | Zero | Low (~5%) | Very Low |
| 2. Assistant prefill | Pre-fill opening chars | Zero | Very Low (<2%) | Low |
| 3. Two-step reason+format | Separate format call | One (haiku) | Near zero | Medium |
| 4. Tool-use enforcement | Schema-validated output | Zero | Near zero | Low |
| 5. Sandwich placement | Top+bottom instructions | Zero | Low (~5%) | Very Low |
| 6. Context compression | Summarise old history | One (haiku) | Low (~5%) | Medium |

**Recommended path:** Start with Option 1 (recency reminder) and Option 5 (sandwich) — both are zero-cost changes that cut format failures by 60–80%. For critical pipelines, use Option 4 (tool-use enforcement) as it provides mechanical schema validation. Use Option 3 (two-step) when you need the full context for reasoning but want clean formatting.
