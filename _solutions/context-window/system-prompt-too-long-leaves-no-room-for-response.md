---
layout: solution
title: "System Prompt Too Long — Leaves No Room for Model Response"
category: context-window
description: "System prompt is 80,000 tokens. Model context window is 100,000 tokens. Only 20,000 tokens remain for conversation history and response. Agent truncates output mid-sentence. Long tasks fail because there is no room for tool results or reasoning."
tags: [context-window, system-prompt, token-budget, truncation, compression, prompt-optimization, caching]
---

# System Prompt Too Long — Leaves No Room for Model Response

## Symptom
- Agent response ends mid-sentence with no apparent reason
- `stop_reason: "max_tokens"` when `max_tokens` was not reached — context window was full
- Tool results return but the model has no room to reason or respond
- System prompt contains entire documentation, all examples, and all rules in one block
- Token counter shows 95,000 / 100,000 used before the first user message
- Reducing `max_tokens` doesn't help — the input is already consuming the window

## Root Cause
The model context window is shared between: system prompt, conversation history, tool definitions, tool results, and the model's response. Every token in the system prompt is a token unavailable for everything else. A 50,000-token system prompt on a 100k-context model leaves only 50,000 tokens for the rest. System prompts often grow over time as engineers add more rules, more examples, and more edge-case handling — with no one tracking the total size.

## Fix

### Option 1: Measure and budget tokens explicitly

```python
import anthropic

client = anthropic.Anthropic()

def count_tokens(text: str, model: str = "claude-sonnet-4-6") -> int:
    """Count tokens for a string using the Anthropic token counting API"""
    response = client.beta.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": text}],
        betas=["token-counting-2024-11-01"]
    )
    return response.input_tokens

def audit_prompt_budget(
    system_prompt: str,
    model: str = "claude-sonnet-4-6",
    context_window: int = 200_000,
    target_response_tokens: int = 4_096,
    target_history_tokens: int = 20_000
) -> dict:
    """
    Audit system prompt token usage and report budget breakdown.
    """
    system_tokens = count_tokens(system_prompt, model)
    overhead = 500  # Tool definitions, formatting, etc.

    available_for_response = context_window - system_tokens - target_history_tokens - overhead
    budget_ok = available_for_response >= target_response_tokens

    report = {
        "context_window": context_window,
        "system_prompt_tokens": system_tokens,
        "system_prompt_pct": round(system_tokens / context_window * 100, 1),
        "target_history_tokens": target_history_tokens,
        "available_for_response": available_for_response,
        "target_response_tokens": target_response_tokens,
        "budget_ok": budget_ok,
    }

    if not budget_ok:
        report["warning"] = (
            f"System prompt uses {report['system_prompt_pct']}% of context window. "
            f"Only {available_for_response} tokens remain for responses. "
            f"Need at least {target_response_tokens}. Reduce system prompt by "
            f"{target_response_tokens - available_for_response} tokens."
        )

    return report

# Run before deploying:
audit = audit_prompt_budget(YOUR_SYSTEM_PROMPT)
print(f"System prompt: {audit['system_prompt_tokens']} tokens ({audit['system_prompt_pct']}%)")
if not audit["budget_ok"]:
    print(f"WARNING: {audit['warning']}")
```

### Option 2: Dynamic system prompt — include only what's needed

```python
from dataclasses import dataclass

@dataclass
class SystemPromptBuilder:
    """
    Build system prompt dynamically based on the current task.
    Include only the sections relevant to the active task type.
    """

    CORE_IDENTITY = """You are a helpful AI assistant."""  # ~10 tokens — always include

    TOOL_INSTRUCTIONS = {
        "search": "When searching, always verify sources before citing them.",  # ~15 tokens
        "code": "When writing code, include error handling and type annotations.",
        "math": "Show all calculation steps. Double-check arithmetic before responding.",
        "email": "Always confirm the recipient before sending. Use professional tone.",
        "database": "Never run DROP or DELETE without explicit user confirmation.",
    }

    DOMAIN_KNOWLEDGE = {
        "medical": "You are assisting medical professionals. Always recommend consulting a doctor.",
        "legal": "You are assisting legal professionals. Always recommend consulting an attorney.",
        "finance": "You are assisting financial professionals. Always note this is not financial advice.",
    }

    EXAMPLES = {
        "code_review": "Example good review:\n...",  # Only include when doing code review
        "data_analysis": "Example analysis:\n...",  # Only include for data tasks
    }

    def build(
        self,
        task_type: str | None = None,
        domain: str | None = None,
        include_examples: bool = False
    ) -> str:
        sections = [self.CORE_IDENTITY]

        if task_type and task_type in self.TOOL_INSTRUCTIONS:
            sections.append(self.TOOL_INSTRUCTIONS[task_type])

        if domain and domain in self.DOMAIN_KNOWLEDGE:
            sections.append(self.DOMAIN_KNOWLEDGE[domain])

        if include_examples and task_type and task_type in self.EXAMPLES:
            sections.append(self.EXAMPLES[task_type])

        return "\n\n".join(sections)

builder = SystemPromptBuilder()

# Short prompt for simple tasks:
simple_prompt = builder.build()  # ~10 tokens

# Targeted prompt for code review:
code_prompt = builder.build(task_type="code_review", include_examples=True)  # ~200 tokens

# Instead of one 10,000-token prompt for all tasks:
# Pick the right sections for the right task
```

### Option 3: Compress system prompt — remove redundancy

```python
import re

def compress_system_prompt(prompt: str) -> str:
    """
    Reduce system prompt size by removing common redundancies.
    Safe transformations that preserve meaning.
    """
    # Remove excessive blank lines (more than 2 in a row)
    prompt = re.sub(r'\n{3,}', '\n\n', prompt)

    # Remove trailing whitespace on each line
    prompt = '\n'.join(line.rstrip() for line in prompt.split('\n'))

    # Remove obviously redundant phrases
    redundant_phrases = [
        ("You are an AI assistant. You should ", ""),
        ("Please make sure to always ", "Always "),
        ("It is important that you ", ""),
        ("You must remember to ", ""),
        ("When you are responding, ", "When responding, "),
        ("In the event that ", "If "),
        ("At all times, you should ", "Always "),
        ("Note that it is critical that ", "Critical: "),
    ]

    for old, new in redundant_phrases:
        prompt = prompt.replace(old, new)

    return prompt.strip()

def split_prompt_into_sections(prompt: str) -> dict[str, str]:
    """
    Parse a system prompt into labeled sections for selective inclusion.
    Sections marked with ## headers can be included/excluded independently.
    """
    sections = {}
    current_section = "intro"
    current_content = []

    for line in prompt.split('\n'):
        if line.startswith('## '):
            if current_content:
                sections[current_section] = '\n'.join(current_content).strip()
            current_section = line[3:].lower().replace(' ', '_')
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections[current_section] = '\n'.join(current_content).strip()

    return sections

# Measure before and after:
original = load_system_prompt()
compressed = compress_system_prompt(original)
sections = split_prompt_into_sections(compressed)

original_tokens = count_tokens(original)
compressed_tokens = count_tokens(compressed)
print(f"Compression: {original_tokens} → {compressed_tokens} tokens "
      f"({(1 - compressed_tokens/original_tokens)*100:.1f}% reduction)")
```

### Option 4: Prompt caching — reuse expensive system prompt tokens

```python
import anthropic

client = anthropic.Anthropic()

def create_with_cached_system_prompt(
    system_prompt: str,
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096
) -> anthropic.types.Message:
    """
    Use prompt caching to cache the system prompt.
    After the first call, the system prompt tokens are cached — billed at 10% cost.
    The context window limit still applies, but cache hits are fast and cheap.
    """
    response = client.beta.messages.create(
        model=model,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}  # Cache this block
            }
        ],
        messages=messages,
        max_tokens=max_tokens,
        betas=["prompt-caching-2024-07-31"]
    )

    # Log cache usage
    usage = response.usage
    if hasattr(usage, 'cache_read_input_tokens') and usage.cache_read_input_tokens:
        print(
            f"Cache hit: {usage.cache_read_input_tokens} cached tokens "
            f"(saved ~{usage.cache_read_input_tokens * 0.9:.0f} tokens in cost)"
        )
    elif hasattr(usage, 'cache_creation_input_tokens') and usage.cache_creation_input_tokens:
        print(f"Cache created: {usage.cache_creation_input_tokens} tokens written to cache")

    return response

# Note: caching reduces COST but not context window usage.
# To reduce context window usage, you must reduce prompt SIZE.
# Use both: cache large-but-necessary prompts, AND minimize unnecessary content.
```

### Option 5: RAG — move knowledge to retrieval instead of system prompt

```python
from dataclasses import dataclass

@dataclass
class RAGSystemPrompt:
    """
    Replace large static knowledge in system prompt with on-demand retrieval.
    System prompt shrinks from 50k tokens to 2k tokens.
    Relevant knowledge is fetched and injected per request.
    """

    # Short system prompt — no inline knowledge base
    BASE_SYSTEM = """You are a helpful assistant.
When you need information about products, policies, or procedures,
the relevant documentation will be provided in the conversation."""

    def __init__(self, vector_store):
        self.store = vector_store

    async def build_context_for_query(self, user_query: str, top_k: int = 3) -> str:
        """
        Retrieve relevant documents for the user's query.
        Returns formatted context to inject into the conversation.
        """
        results = await self.store.search(user_query, top_k=top_k)

        if not results:
            return ""

        context_sections = []
        for doc in results:
            context_sections.append(
                f"### {doc.title}\n{doc.content[:2000]}"  # Limit per-doc tokens
            )

        context = "\n\n".join(context_sections)
        return f"Relevant documentation:\n\n{context}"

    async def chat(self, user_message: str, history: list[dict], client) -> str:
        # Retrieve relevant context
        context = await self.build_context_for_query(user_message)

        # Inject context as a user-turn prefix (not in system prompt)
        augmented_messages = list(history)
        if context:
            # Add retrieved context before the user message
            if augmented_messages and augmented_messages[-1]["role"] == "user":
                augmented_messages[-1]["content"] = f"{context}\n\n{user_message}"
            else:
                augmented_messages.append({"role": "user", "content": f"{context}\n\n{user_message}"})
        else:
            augmented_messages.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            system=self.BASE_SYSTEM,  # Short — 50 tokens
            messages=augmented_messages,
            max_tokens=4096
        )
        return response.content[0].text

# Before RAG: 50,000 token system prompt (entire knowledge base)
# After RAG: 50 token system prompt + ~2,000 tokens of retrieved context per query
```

### Option 6: Token budget enforcement in CI/CD

```python
import sys
import anthropic

MAX_SYSTEM_PROMPT_TOKENS = 8_000  # Enforce a hard cap

def check_system_prompt_size(prompt_path: str) -> int:
    """
    CI/CD check: fail the build if system prompt exceeds token budget.
    Run this in pre-commit hooks or CI pipelines.
    """
    with open(prompt_path) as f:
        prompt = f.read()

    client = anthropic.Anthropic()
    response = client.beta.messages.count_tokens(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": prompt}],
        betas=["token-counting-2024-11-01"]
    )
    token_count = response.input_tokens

    print(f"System prompt: {token_count:,} tokens (limit: {MAX_SYSTEM_PROMPT_TOKENS:,})")

    if token_count > MAX_SYSTEM_PROMPT_TOKENS:
        print(
            f"FAIL: System prompt exceeds limit by "
            f"{token_count - MAX_SYSTEM_PROMPT_TOKENS:,} tokens. "
            f"Reduce before merging."
        )
        sys.exit(1)

    utilization = token_count / MAX_SYSTEM_PROMPT_TOKENS * 100
    print(f"OK: {utilization:.1f}% of token budget used")
    return token_count

if __name__ == "__main__":
    check_system_prompt_size("prompts/system.txt")

# Add to .pre-commit-config.yaml:
# - repo: local
#   hooks:
#   - id: check-system-prompt-tokens
#     name: Check system prompt token count
#     entry: python scripts/check_prompt_tokens.py
#     language: python
#     files: prompts/system.txt
```

## Token Budget Allocation Guidelines

| Component | Recommended Budget | Notes |
|---|---|---|
| System prompt | 5–15% of context window | Should rarely exceed 10% |
| Tool definitions | 1–5% | Grows with tool count |
| Conversation history | 40–60% | Sliding window manages this |
| Current user input | 5–15% | Varies by task |
| Reserved for response | 10–20% | Must be explicitly reserved |
| Buffer | 5% | Safety margin |

## Context Window Sizes (2025)

| Model | Context Window | Safe System Prompt Budget |
|---|---|---|
| claude-haiku-4-5 | 200,000 | ~20,000 tokens |
| claude-sonnet-4-6 | 200,000 | ~20,000 tokens |
| claude-opus-4-6 | 200,000 | ~20,000 tokens |

## Expected Token Savings
System prompt at 80% of context → output truncated → user must re-request: ~8,000 tokens wasted per truncation
Compressed system prompt at 10% → full context available for reasoning and response: 0 truncations

## Environment
- Any agent with feature-rich system prompts; critical for agents that have grown organically with rules added over months without token auditing
- Source: direct experience; system prompt bloat is the most common cause of unexplained response truncation in mature agent deployments
