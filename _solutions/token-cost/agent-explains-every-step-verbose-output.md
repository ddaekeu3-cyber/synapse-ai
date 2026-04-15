---
layout: solution
title: "Agent Produces Verbose Step-by-Step Explanations Nobody Asked For — Token Waste"
category: token-cost
description: "Agent explains every reasoning step, provides lengthy preambles, re-states the problem, and adds closing summaries. A 10-line fix comes with 500 tokens of explanation nobody needs."
tags: [verbose, token-waste, concise, output-format, efficiency]
---

# Agent Produces Verbose Step-by-Step Explanations Nobody Asked For — Token Waste

## Symptom
- Every response starts with "Certainly! Let me help you with..."
- Agent restates the user's question before answering it
- A simple function comes with a 200-word explanation of what it does
- Agent adds "In summary:" sections at the end recapping what it just said
- "Let me think through this step by step..." before every answer
- Response is 90% explanation, 10% actual output

## Root Cause
LLMs are trained on helpful examples where thorough explanation is valued. Without explicit instruction to be concise, the model defaults to thorough — especially for safety (showing reasoning) and helpfulness (making sure you understand). In a production agent context, this verbose output costs tokens with no value.

## Fix

### Option 1: Direct brevity instruction

```
System prompt:
"Communication style:
- Be concise. Skip preambles, summaries, and unnecessary explanation.
- Never start with 'Certainly!', 'Of course!', 'Sure!', or 'Great question!'
- Never restate the user's question
- Never add 'In summary:' or 'To recap:' sections
- For code requests: write the code, add ONE sentence explanation if needed
- If the answer is a code block, start with the code block

The user can read the code — don't explain what it does unless asked."
```

### Option 2: Output format constraints

```
System prompt:
"Output format rules:
- Code requests: code first, brief comment only if non-obvious logic
- Questions: direct answer first, supporting details only if necessary
- Errors/debug: exact fix + one-line reason
- Maximum explanation: 3 sentences before a code block

Count your sentences before responding. If you have more than 3 sentences
of explanation without any code/output, you are being too verbose."
```

### Option 3: Measure and cap output tokens

```python
from anthropic import Anthropic

client = Anthropic()

# Use max_tokens to enforce brevity
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=500,  # Force concise responses for simple tasks
    system="Be extremely concise. Code first, minimal explanation.",
    messages=[{"role": "user", "content": user_message}]
)
```

### Option 4: Strip verbose patterns programmatically

```python
import re

VERBOSE_PATTERNS = [
    r'^(Certainly|Of course|Sure|Absolutely|Great question)[!,]?\s*',
    r'^Let me (help you|think|walk you through|explain)\s.*?\.\s*',
    r'In summary[,:].*?(?=\n\n|\Z)',
    r'To recap[,:].*?(?=\n\n|\Z)',
    r'I hope this helps.*?$',
    r'Let me know if you (have|need).*?$',
]

def strip_verbose(text):
    for pattern in VERBOSE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return text.strip()
```

### Option 5: Different models for different verbosity needs

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    model_routing:
      quick_answer: claude-haiku-4-5-20251001   # Naturally more concise
      standard_task: claude-sonnet-4-6
      complex_analysis: claude-opus-4-6          # Allow more tokens for complex work
```

Haiku is naturally less verbose than Opus. For simple Q&A and code generation, route to Haiku.

## Token Cost Comparison

| Response style | Tokens | Contains |
|---------------|--------|---------|
| Verbose | 800 | 200 useful + 600 explanation |
| Concise | 250 | 200 useful + 50 context |
| Ultra-concise | 210 | 200 useful + 10 |

For 100 agent interactions/day: verbose = 80,000 tokens, concise = 25,000 tokens — 69% reduction.

## Expected Token Savings
Per 100 interactions with brevity instructions: ~55,000 tokens saved
System prompt overhead: ~100 tokens

## Environment
- Any user-facing agent or chat interface
- Source: direct experience, measurable in production logs
