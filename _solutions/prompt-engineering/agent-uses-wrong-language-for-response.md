---
layout: solution
title: "Agent Responds in Wrong Language — Ignores User's Input Language"
category: prompt-engineering
description: "User writes in Spanish. Agent responds in English. User writes in Japanese. Agent responds in English anyway. No language detection. System prompt says nothing about language. Agent always defaults to English regardless of input."
tags: [prompt-engineering, language, localization, detection, multilingual, i18n, system-prompt]
---

# Agent Responds in Wrong Language — Ignores User's Input Language

## Symptom
- User writes in Spanish, agent responds in English every time
- Agent correctly answers the question but in the wrong language
- Switching languages mid-conversation resets to English by default
- System prompt specifies English — agent ignores all non-English input
- Agent translates non-English queries to English before processing, discards original language
- Multi-language product ships with agent that only speaks English

## Root Cause
Language handling is not specified in the system prompt, so the model defaults to English — its dominant training language. Without an explicit instruction to match the user's input language, the model treats English as the "neutral" output language. Many system prompts specify the persona or task but say nothing about language, leaving the model to guess. Additionally, if the system prompt is written in English, the model anchors on English even when the user writes in another language.

## Fix

### Option 1: System prompt — explicit language matching instruction

```
System prompt:
"You are a helpful assistant.

LANGUAGE RULE (highest priority):
- ALWAYS respond in the SAME language the user uses in their most recent message.
- If the user writes in Spanish, respond entirely in Spanish.
- If the user writes in Japanese, respond entirely in Japanese.
- If the user writes in a mix of languages, match the primary language.
- If you cannot determine the language, ask: 'What language would you prefer?'
- Do NOT default to English unless the user writes in English.
- Do NOT translate the user's message before responding — respond in their language directly."
```

### Option 2: Detect language programmatically and inject into prompt

```python
from langdetect import detect, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException
import anthropic

# Seed for reproducibility (langdetect is non-deterministic by default)
DetectorFactory.seed = 42

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "ja": "Japanese", "ko": "Korean", "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)", "pt": "Portuguese", "it": "Italian",
    "ru": "Russian", "ar": "Arabic", "hi": "Hindi", "nl": "Dutch",
    "pl": "Polish", "sv": "Swedish", "tr": "Turkish", "vi": "Vietnamese",
}

def detect_language(text: str) -> tuple[str, str]:
    """
    Detect language of text.
    Returns (language_code, language_name).
    Falls back to English if detection fails or text is too short.
    """
    if len(text.strip()) < 5:
        return "en", "English"  # Too short to detect reliably

    try:
        code = detect(text)
        name = LANGUAGE_NAMES.get(code, code.upper())
        return code, name
    except LangDetectException:
        return "en", "English"

def build_language_aware_system_prompt(base_prompt: str, user_message: str) -> str:
    """
    Inject a language instruction into the system prompt based on detected language.
    """
    lang_code, lang_name = detect_language(user_message)

    language_instruction = (
        f"\n\nIMPORTANT: The user is writing in {lang_name} ({lang_code}). "
        f"You MUST respond entirely in {lang_name}. "
        f"Do not use any other language in your response."
    )

    return base_prompt + language_instruction

client = anthropic.Anthropic()
BASE_SYSTEM = "You are a helpful customer support agent for a software company."

def chat(user_message: str, history: list[dict]) -> str:
    system = build_language_aware_system_prompt(BASE_SYSTEM, user_message)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=history + [{"role": "user", "content": user_message}],
        max_tokens=1024
    )
    return response.content[0].text

# Test:
print(chat("¿Cómo puedo restablecer mi contraseña?", []))
# → Response in Spanish

print(chat("パスワードをリセットするにはどうすればいいですか？", []))
# → Response in Japanese
```

### Option 3: Prefix injection — add language hint to each user message

```python
from langdetect import detect

def inject_language_prefix(user_message: str) -> str:
    """
    Prepend a language directive to the user message.
    The model sees this as part of the user turn — strong signal.
    """
    try:
        lang_code = detect(user_message)
        lang_names = {
            "es": "Spanish", "fr": "French", "de": "German", "ja": "Japanese",
            "ko": "Korean", "zh-cn": "Chinese", "pt": "Portuguese", "it": "Italian",
            "ru": "Russian", "ar": "Arabic",
        }
        lang_name = lang_names.get(lang_code)
        if lang_name:
            return f"[Respond in {lang_name}]\n{user_message}"
    except Exception:
        pass
    return user_message

# Before adding to history:
user_input = "Cómo instalo el paquete?"
prefixed = inject_language_prefix(user_input)
# → "[Respond in Spanish]\nCómo instalo el paquete?"

# The model now has a strong inline directive
```

### Option 4: Track language across conversation turns

```python
from dataclasses import dataclass, field
from langdetect import detect

@dataclass
class MultilingualConversation:
    """
    Track language across conversation turns.
    Maintains language consistency unless user explicitly switches.
    """
    base_system: str
    history: list[dict] = field(default_factory=list)
    detected_language: str = "en"
    detected_language_name: str = "English"
    language_confidence_turns: int = 0  # How many turns confirmed this language

    def update_language(self, text: str):
        """Update detected language from new user message"""
        try:
            new_lang = detect(text)
            lang_names = {
                "en": "English", "es": "Spanish", "fr": "French",
                "de": "German", "ja": "Japanese", "ko": "Korean",
                "zh-cn": "Chinese", "pt": "Portuguese",
            }
            if new_lang != self.detected_language:
                # Language changed — reset confidence
                print(
                    f"Language switch detected: {self.detected_language_name} → "
                    f"{lang_names.get(new_lang, new_lang)}"
                )
                self.language_confidence_turns = 1
            else:
                self.language_confidence_turns += 1

            self.detected_language = new_lang
            self.detected_language_name = lang_names.get(new_lang, new_lang.upper())
        except Exception:
            pass  # Keep current language on detection failure

    def get_system_prompt(self) -> str:
        return (
            f"{self.base_system}\n\n"
            f"Respond in {self.detected_language_name}. "
            f"Maintain this language for the entire conversation unless the user switches languages."
        )

    def send(self, user_message: str, client) -> str:
        self.update_language(user_message)
        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            system=self.get_system_prompt(),
            messages=self.history,
            max_tokens=1024
        )

        assistant_text = response.content[0].text
        self.history.append({"role": "assistant", "content": assistant_text})
        return assistant_text

import anthropic
client = anthropic.Anthropic()
conv = MultilingualConversation(base_system="You are a helpful assistant.")

r1 = conv.send("Hola, necesito ayuda con mi cuenta.", client)
r2 = conv.send("¿Puedo cambiar mi email?", client)
# Both responses in Spanish

r3 = conv.send("Actually, can we switch to English?", client)
# Language auto-detected as English — response in English
```

### Option 5: FastAPI middleware for API-level language detection

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic
from langdetect import detect

app = FastAPI()
client = anthropic.Anthropic()

BASE_SYSTEM = "You are a helpful AI assistant."

LANGUAGE_MAP = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "ja": "Japanese", "ko": "Korean", "zh-cn": "Chinese (Simplified)",
    "pt": "Portuguese", "it": "Italian", "ru": "Russian",
    "ar": "Arabic", "hi": "Hindi",
}

@app.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_message = body.get("message", "")
    history = body.get("history", [])

    # Detect language from user message
    try:
        lang_code = detect(user_message)
        lang_name = LANGUAGE_MAP.get(lang_code, lang_code.upper())
    except Exception:
        lang_code, lang_name = "en", "English"

    # Inject language instruction
    system = (
        f"{BASE_SYSTEM}\n\n"
        f"The user is writing in {lang_name}. "
        f"Respond ONLY in {lang_name}."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=history + [{"role": "user", "content": user_message}],
        max_tokens=1024
    )

    return JSONResponse({
        "response": response.content[0].text,
        "detected_language": lang_code,
        "detected_language_name": lang_name
    })
```

### Option 6: Language validation — verify response matches expected language

```python
from langdetect import detect

def verify_response_language(response: str, expected_lang: str) -> dict:
    """
    Verify that the model's response matches the expected language.
    Returns validation result with details.
    """
    if len(response.strip()) < 10:
        return {"valid": True, "reason": "too short to validate"}

    try:
        actual_lang = detect(response)
        match = actual_lang == expected_lang

        if not match:
            return {
                "valid": False,
                "expected": expected_lang,
                "actual": actual_lang,
                "warning": (
                    f"Response language mismatch: expected {expected_lang}, "
                    f"got {actual_lang}. Regenerating with stronger language directive."
                )
            }
        return {"valid": True, "language": actual_lang}
    except Exception as e:
        return {"valid": True, "reason": f"detection failed: {e}"}

async def chat_with_language_validation(
    user_message: str,
    history: list[dict],
    client,
    max_retries: int = 2
) -> str:
    """
    Chat with language validation — retries if wrong language detected.
    """
    expected_lang, lang_name = detect_language(user_message)

    for attempt in range(max_retries + 1):
        # Stronger directive on retries
        lang_directive = (
            f"CRITICAL: You MUST respond ONLY in {lang_name}. "
            f"No English. No other languages. Only {lang_name}."
            if attempt > 0
            else f"Respond in {lang_name}."
        )

        system = f"{BASE_SYSTEM}\n\n{lang_directive}"

        response = client.messages.create(
            model="claude-sonnet-4-6",
            system=system,
            messages=history + [{"role": "user", "content": user_message}],
            max_tokens=1024
        )

        text = response.content[0].text
        validation = verify_response_language(text, expected_lang)

        if validation["valid"]:
            return text

        print(f"Attempt {attempt + 1}: {validation['warning']}")

    return text  # Return last attempt even if wrong language
```

## Language Detection Library Options

| Library | Accuracy | Speed | Languages | Notes |
|---|---|---|---|---|
| `langdetect` | High | Fast | 55+ | Non-deterministic, seed for reproducibility |
| `langid` | Medium | Fastest | 97 | Deterministic, good for short text |
| `fasttext` (Meta) | Very high | Fast | 176 | Best for short messages, requires model download |
| `lingua-py` | Highest | Medium | 75 | Best accuracy for short text, pure Python |
| Claude itself | Very high | LLM speed | All | Ask model to detect: "What language is: '{text}'?" |

## Expected Token Savings
User complains about language, clarifies, re-asks in English: ~3,000 tokens per wrong-language interaction
Correct language on first response: 0 wasted tokens per interaction — multiplied across all users

## Environment
- Any agent deployed to multilingual or international users; critical for consumer products with global audiences
- Source: direct experience; language defaulting to English is the #1 complaint from non-English-speaking users of AI agents
