---
layout: solution
title: "Agent Responds in Wrong Language — Switches to English When Korean Expected or Vice Versa"
category: prompt-engineering
description: "Agent switches language unexpectedly: responds in English when user messages are in Korean, or reverts to English mid-conversation. Language consistency fails across sessions."
tags: [language, localization, korean, english, prompt-design]
---

# Agent Responds in Wrong Language — Switches to English When Korean Expected or Vice Versa

## Symptom
- User sends messages in Korean, agent responds in English
- Agent responds in the correct language for first few turns, then switches
- After context pruning, agent reverts to its default language
- Tool results in English cause agent to switch to English for the rest of session
- System prompt is in English but user expects Korean responses

## Root Cause
The model defaults to matching the language of the most recent content in context. If tool results, system prompt, or injected context is in English, the model may switch to English even when the user writes in Korean. Language instruction far from the generation point loses influence (same as instruction drift).

## Fix

### Option 1: Explicit language instruction in system prompt

```
# For Korean-first agents
System prompt:
"LANGUAGE RULE: Always respond in Korean (한국어) regardless of:
- What language the system prompt is written in
- What language tool results are in
- What language any injected context is in

Only exception: if the user explicitly switches language mid-conversation.
Never switch language without an explicit user request."
```

### Option 2: Language instruction at the END of system prompt

Since models attend more to the end of the system prompt under long contexts:

```python
BASE_INSTRUCTIONS = """
[... all other instructions ...]
"""

LANGUAGE_ANCHOR = """
CRITICAL — LANGUAGE: Respond ONLY in Korean (한국어).
This applies to every response regardless of input language.
"""

SYSTEM_PROMPT = BASE_INSTRUCTIONS + "\n" + LANGUAGE_ANCHOR
```

### Option 3: Wrap tool results with language reminder

```python
def wrap_tool_result_with_language(result, language="Korean"):
    return f"""[Tool result below — continue responding in {language}]
{result}
[End of tool result — your response must be in {language}]"""
```

### Option 4: Language detection and correction

```python
from langdetect import detect

def check_response_language(response, expected_lang="ko"):
    """Verify response is in expected language"""
    try:
        detected = detect(response)
        if detected != expected_lang:
            return False, detected
        return True, detected
    except Exception:
        return True, None  # Can't detect — assume correct

async def get_language_correct_response(messages, system, expected_lang="ko"):
    response = await client.messages.create(system=system, messages=messages)
    text = response.content[0].text

    is_correct, detected = check_response_language(text, expected_lang)
    if not is_correct:
        # Retry with stronger language instruction
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"[System: Your response was in {detected}. Please respond in Korean.]"}
        ]
        response = await client.messages.create(system=system, messages=messages)

    return response
```

### Option 5: Match system prompt language to expected output language

If users expect Korean, write the system prompt in Korean:

```
# Korean system prompt for Korean-speaking users
시스템 프롬프트:
"당신은 SynapseAI의 기술 지원 에이전트입니다.
모든 응답은 반드시 한국어로 해야 합니다.
영어 도구 결과를 받더라도 사용자에게는 항상 한국어로 답변하세요."
```

## Language Code Reference

| Language | Code | Instruction phrase |
|----------|------|-------------------|
| Korean | ko | 항상 한국어로 응답 |
| English | en | Always respond in English |
| Japanese | ja | 常に日本語で回答 |
| Chinese | zh | 始终用中文回答 |

## Expected Token Savings
Fixing wrong-language responses and re-asking: ~3,000 tokens per incident
Language anchor in system prompt: prevents the issue entirely

## Environment
- Any multilingual agent deployment
- Higher risk: system prompt in different language than user messages
- Source: direct experience with Korean-English language switching
