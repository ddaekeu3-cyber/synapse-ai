---
layout: solution
title: "Agent Outputs Markdown Formatting When Plain Text Is Required"
category: general
description: "Agent wraps everything in **bold**, ```code blocks```, and ## headers even when the output goes into a plain-text field, API response, or system that doesn't render markdown."
tags: [markdown, plain-text, formatting, output, api-response]
---

# Agent Outputs Markdown Formatting When Plain Text Is Required

## Symptom
- API response contains `**bold**`, `## headers`, ` ```python ``` ` as literal text
- Database field stores `- bullet point` instead of clean text
- Email body shows `\n\n## Summary\n\n` instead of rendered formatting
- CLI tool output is cluttered with `*asterisks*` and `#` symbols
- Downstream system receiving agent output breaks on markdown syntax

## Root Cause
Claude defaults to markdown formatting — it's trained to produce well-structured responses. Without explicit instruction to suppress markdown, the model will use it whenever the output would benefit from structure. The model doesn't know whether the output destination renders markdown or not.

## Fix

### Option 1: Explicit plain text instruction in system prompt

```
System prompt:
"Output format: PLAIN TEXT ONLY.
Do not use markdown formatting of any kind:
- No **bold** or *italic*
- No ## headers or # titles
- No bullet points with dashes or asterisks (use plain numbered lists or commas)
- No ```code blocks``` (write code inline or use indentation only)
- No > blockquotes
- No --- horizontal rules
- No [links](url) — write URLs as plain text

Respond as if writing to a plain text file that will be read by a machine."
```

### Option 2: Context-specific format instructions

```python
FORMAT_INSTRUCTIONS = {
    "plain_text": "Use plain text only. No markdown formatting.",
    "markdown": "Use markdown formatting with headers, bullets, and code blocks.",
    "json": "Return only valid JSON. No explanation text, no markdown.",
    "html": "Return HTML only. No markdown. Use <p>, <ul>, <code> tags.",
    "slack": "Use Slack mrkdwn: *bold*, _italic_, `code`, ```code block```. No ## headers.",
    "email": "Write as a professional plain-text email. No markdown.",
}

def build_system_prompt(output_format: str) -> str:
    base = "You are a helpful assistant."
    format_rule = FORMAT_INSTRUCTIONS.get(output_format, "")
    return f"{base}\n\n{format_rule}"
```

### Option 3: Strip markdown from output post-processing

```python
import re

def strip_markdown(text: str) -> str:
    """Remove common markdown formatting from text"""
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', text)
    # Remove code blocks (keep content)
    text = re.sub(r'```[\w]*\n?(.*?)```', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove bullet points
    text = re.sub(r'^[\-\*\+]\s+', '', text, flags=re.MULTILINE)
    # Remove blockquotes
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
    # Remove link formatting (keep text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

response_text = strip_markdown(agent_response)
```

### Option 4: Ask for specific output structure

```python
# Instead of asking for "a summary", ask for a specific plain format
user_message = """
Summarize the following in exactly 3 sentences.
Return only the 3 sentences as plain text, separated by spaces.
No bullet points, no headers, no formatting.

Text to summarize:
{text}
""".format(text=document)
```

### Option 5: Validate output format before using it

```python
import re

def has_markdown(text: str) -> bool:
    """Detect if text contains markdown formatting"""
    patterns = [
        r'\*{1,3}\S',          # bold/italic
        r'^#{1,6}\s',          # headers (multiline)
        r'^```',               # code blocks
        r'`[^`]+`',           # inline code
        r'^\s*[-\*\+]\s',     # bullet lists
        r'\[.+\]\(.+\)',       # links
    ]
    for pattern in patterns:
        if re.search(pattern, text, re.MULTILINE):
            return True
    return False

response = agent.complete(prompt)
if has_markdown(response):
    # Re-request with explicit plain text instruction
    response = agent.complete(
        prompt + "\n\nIMPORTANT: Your previous response contained markdown formatting. "
                 "Rewrite using plain text only."
    )
```

## Format Reference by Destination

| Output destination | Use format |
|---|---|
| REST API JSON field | Plain text |
| Email body (plain) | Plain text |
| Database text column | Plain text |
| Terminal / CLI | Plain text or ANSI colors |
| GitHub issue/PR | Markdown |
| Slack message | Slack mrkdwn |
| Web page (rendered) | HTML or Markdown |
| Jupyter notebook | Markdown |
| PDF report | Plain text or HTML |

## Expected Token Savings
Re-requesting stripped output: ~500 tokens
System prompt instruction prevents it upfront: 0 extra tokens

## Environment
- Any agent whose output is consumed by systems that don't render markdown
- Source: direct experience with API integrations, email pipelines, database writes
