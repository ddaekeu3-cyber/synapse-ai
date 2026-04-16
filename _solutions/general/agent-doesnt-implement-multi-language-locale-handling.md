---
layout: solution
title: "Agent Doesn't Implement Multi-Language Locale Handling"
description: "How to detect user language, adapt agent responses to the correct locale, and handle multilingual inputs consistently — without hardcoding language assumptions into every prompt."
tags: [general, internationalization, locale, language, i18n, multilingual]
difficulty: intermediate
solution_count: 6
---

## Problem

Agents assume English input and output. A French user receives English responses, a Japanese user's date format is misinterpreted, and a right-to-left language user sees broken formatting. Localizing the agent is an afterthought: language detection is hacked into individual prompts, currency and date formats are inconsistent, and adding a new language requires modifying dozens of files.

```python
# Bad: hardcoded English assumptions
system_prompt = "You are a helpful assistant. Always respond in English."
# French user: "Aidez-moi s'il vous plaît" → gets English response
# Japanese user: date "2024/3/15" → agent misparses as month=3, day=15 in US format
```

---

## Solution 1 — Language Detection and Dynamic System Prompt

Detect the user's language from their message and inject the correct language instruction into the system prompt dynamically.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class LocaleContext:
    language_code: str   # e.g., "fr", "ja", "ar"
    language_name: str   # e.g., "French", "Japanese", "Arabic"
    is_rtl: bool
    date_format: str     # e.g., "DD/MM/YYYY", "YYYY年MM月DD日"
    number_format: str   # e.g., "1.234,56" vs "1,234.56"

LOCALE_METADATA: dict[str, LocaleContext] = {
    "en": LocaleContext("en", "English", False, "MM/DD/YYYY", "1,234.56"),
    "fr": LocaleContext("fr", "French", False, "DD/MM/YYYY", "1 234,56"),
    "de": LocaleContext("de", "German", False, "DD.MM.YYYY", "1.234,56"),
    "ja": LocaleContext("ja", "Japanese", False, "YYYY年MM月DD日", "1,234.56"),
    "zh": LocaleContext("zh", "Chinese", False, "YYYY年MM月DD日", "1,234.56"),
    "ar": LocaleContext("ar", "Arabic", True, "DD/MM/YYYY", "١٬٢٣٤٫٥٦"),
    "ko": LocaleContext("ko", "Korean", False, "YYYY년 MM월 DD일", "1,234.56"),
    "pt": LocaleContext("pt", "Portuguese", False, "DD/MM/YYYY", "1.234,56"),
    "es": LocaleContext("es", "Spanish", False, "DD/MM/YYYY", "1.234,56"),
    "ru": LocaleContext("ru", "Russian", False, "DD.MM.YYYY", "1 234,56"),
}

async def detect_language(text: str) -> str:
    """Detect language code from user message using LLM."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": (
                f"What language is this text? Reply with ONLY the ISO 639-1 code "
                f"(e.g., 'en', 'fr', 'ja'). Text: {text[:200]!r}"
            )
        }],
    )
    code = response.content[0].text.strip().lower()[:2]
    return code if code in LOCALE_METADATA else "en"

def build_localized_system_prompt(base_prompt: str, locale: LocaleContext) -> str:
    rtl_note = " Note: this language is right-to-left." if locale.is_rtl else ""
    return (
        f"{base_prompt}\n\n"
        f"LANGUAGE INSTRUCTION: Always respond in {locale.language_name}.{rtl_note}\n"
        f"Use the date format: {locale.date_format}\n"
        f"Use the number format: {locale.number_format}"
    )

async def localized_agent_response(
    user_message: str,
    base_system: str,
    preferred_language: str | None = None,
) -> dict:
    lang_code = preferred_language or await detect_language(user_message)
    locale = LOCALE_METADATA.get(lang_code, LOCALE_METADATA["en"])
    system = build_localized_system_prompt(base_system, locale)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return {
        "response": response.content[0].text,
        "detected_language": lang_code,
        "locale": locale,
    }

# Usage
async def demo():
    base_system = "You are a helpful customer service assistant."
    result = await localized_agent_response(
        "Bonjour! Pouvez-vous m'aider avec ma commande?",
        base_system,
    )
    print(f"Language: {result['detected_language']}")
    print(f"Response: {result['response'][:200]}")

asyncio.run(demo())
```

---

## Solution 2 — User Locale Profile Persistence

Store the detected language and locale preferences in the user's session profile. Subsequent requests skip detection and use the stored locale — faster and more consistent.

```python
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from typing import Any
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379", decode_responses=True)

@dataclass
class UserLocaleProfile:
    user_id: str
    language_code: str
    detected_at: float
    detection_confidence: float
    user_confirmed: bool  # True if user explicitly set the language
    timezone: str
    currency_code: str
    override_language: str | None = None  # explicit user preference

PROFILE_KEY = "user:locale:{user_id}"

async def get_or_detect_locale(
    user_id: str,
    message: str,
) -> UserLocaleProfile:
    """Return stored locale profile, or detect and store it."""
    key = PROFILE_KEY.format(user_id=user_id)
    raw = await redis.get(key)
    if raw:
        return UserLocaleProfile(**json.loads(raw))

    # Detect language
    lang_code = await detect_language(message)
    profile = UserLocaleProfile(
        user_id=user_id,
        language_code=lang_code,
        detected_at=time.time(),
        detection_confidence=0.85,
        user_confirmed=False,
        timezone="UTC",
        currency_code=LANG_TO_CURRENCY.get(lang_code, "USD"),
    )
    await redis.setex(key, 86400 * 30, json.dumps(asdict(profile)))
    return profile

async def update_user_language_preference(user_id: str, language_code: str) -> None:
    """User explicitly selected a language — store as confirmed."""
    key = PROFILE_KEY.format(user_id=user_id)
    raw = await redis.get(key)
    if raw:
        profile = UserLocaleProfile(**json.loads(raw))
    else:
        profile = UserLocaleProfile(user_id, language_code, time.time(), 1.0, True, "UTC", "USD")

    profile.language_code = language_code
    profile.user_confirmed = True
    profile.override_language = language_code
    await redis.setex(key, 86400 * 365, json.dumps(asdict(profile)))

LANG_TO_CURRENCY = {
    "en": "USD", "fr": "EUR", "de": "EUR", "ja": "JPY",
    "zh": "CNY", "ko": "KRW", "pt": "BRL", "ru": "RUB",
}

async def session_aware_response(user_id: str, message: str) -> str:
    profile = await get_or_detect_locale(user_id, message)
    lang = profile.override_language or profile.language_code
    locale = LOCALE_METADATA.get(lang, LOCALE_METADATA["en"])

    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=build_localized_system_prompt("You are a helpful assistant.", locale),
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text
```

---

## Solution 3 — Prompt Template Localization Registry

Store localized system prompt templates keyed by language code. The agent selects the right template rather than translating on the fly — more reliable and predictable.

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class LocalizedPromptTemplate:
    language_code: str
    system_prompt: str
    error_messages: dict[str, str]
    ui_strings: dict[str, str]

class PromptLocalizationRegistry:
    def __init__(self):
        self._templates: dict[str, LocalizedPromptTemplate] = {}
        self._fallback = "en"

    def register(self, template: LocalizedPromptTemplate) -> None:
        self._templates[template.language_code] = template

    def get(self, language_code: str) -> LocalizedPromptTemplate:
        return (
            self._templates.get(language_code)
            or self._templates.get(self._fallback)
        )

    def get_system_prompt(self, language_code: str, **variables) -> str:
        template = self.get(language_code)
        try:
            return template.system_prompt.format(**variables)
        except KeyError as e:
            raise ValueError(f"Missing template variable {e} for language '{language_code}'")

    def get_error(self, language_code: str, error_key: str) -> str:
        template = self.get(language_code)
        return template.error_messages.get(error_key, error_key)

registry = PromptLocalizationRegistry()

registry.register(LocalizedPromptTemplate(
    language_code="en",
    system_prompt=(
        "You are a helpful assistant for {company_name}. "
        "Always respond in English. Be concise and professional."
    ),
    error_messages={
        "rate_limit": "You've sent too many messages. Please wait a moment.",
        "tool_failed": "I encountered an error. Please try again.",
        "context_limit": "This conversation is too long. Let's start fresh.",
    },
    ui_strings={
        "thinking": "Thinking...",
        "processing_tools": "Using tools...",
        "done": "Done!",
    },
))

registry.register(LocalizedPromptTemplate(
    language_code="fr",
    system_prompt=(
        "Vous êtes un assistant serviable pour {company_name}. "
        "Répondez toujours en français. Soyez concis et professionnel."
    ),
    error_messages={
        "rate_limit": "Vous avez envoyé trop de messages. Veuillez patienter.",
        "tool_failed": "J'ai rencontré une erreur. Veuillez réessayer.",
        "context_limit": "Cette conversation est trop longue. Recommençons.",
    },
    ui_strings={
        "thinking": "Réflexion en cours...",
        "processing_tools": "Utilisation des outils...",
        "done": "Terminé!",
    },
))

registry.register(LocalizedPromptTemplate(
    language_code="ja",
    system_prompt=(
        "{company_name}のアシスタントです。"
        "常に日本語でお答えします。簡潔でプロフェッショナルな対応を心がけます。"
    ),
    error_messages={
        "rate_limit": "メッセージが多すぎます。少々お待ちください。",
        "tool_failed": "エラーが発生しました。もう一度お試しください。",
        "context_limit": "会話が長すぎます。新しい会話を始めましょう。",
    },
    ui_strings={
        "thinking": "考え中...",
        "processing_tools": "ツールを使用中...",
        "done": "完了!",
    },
))

# Usage
system = registry.get_system_prompt("fr", company_name="Acme Corp")
error_msg = registry.get_error("ja", "rate_limit")
print(f"French system: {system}")
print(f"Japanese error: {error_msg}")
```

---

## Solution 4 — Locale-Aware Date, Number, and Currency Formatting

Intercept agent outputs and reformat dates, numbers, and currencies to the user's locale before delivery.

```python
import re
from datetime import datetime
from dataclasses import dataclass

@dataclass
class LocaleFormatter:
    language_code: str
    date_format: str
    decimal_sep: str
    thousands_sep: str
    currency_symbol: str
    currency_before: bool  # True: "$100", False: "100€"

FORMATTERS: dict[str, LocaleFormatter] = {
    "en": LocaleFormatter("en", "%m/%d/%Y", ".", ",", "$", True),
    "fr": LocaleFormatter("fr", "%d/%m/%Y", ",", "\u202f", "€", False),
    "de": LocaleFormatter("de", "%d.%m.%Y", ",", ".", "€", False),
    "ja": LocaleFormatter("ja", "%Y年%m月%d日", ".", ",", "¥", True),
}

def format_number(value: float, formatter: LocaleFormatter,
                   decimals: int = 2) -> str:
    """Format a number according to locale conventions."""
    integer_part = int(abs(value))
    decimal_part = round(abs(value) - integer_part, decimals)

    # Apply thousands separator
    int_str = ""
    for i, digit in enumerate(reversed(str(integer_part))):
        if i > 0 and i % 3 == 0:
            int_str = formatter.thousands_sep + int_str
        int_str = digit + int_str

    if decimals > 0:
        dec_str = f"{decimal_part:.{decimals}f}"[1:]  # e.g., ".56"
        dec_str = dec_str.replace(".", formatter.decimal_sep)
        result = int_str + dec_str
    else:
        result = int_str

    return ("-" if value < 0 else "") + result

def format_currency(amount: float, formatter: LocaleFormatter) -> str:
    num = format_number(amount, formatter, 2)
    if formatter.currency_before:
        return f"{formatter.currency_symbol}{num}"
    return f"{num}\u00a0{formatter.currency_symbol}"  # non-breaking space

# ISO date pattern in agent output: YYYY-MM-DD
ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Simple currency pattern: $123.45 or USD 123.45
USD_PATTERN = re.compile(r"\$(\d[\d,]*\.?\d*)")

def localize_agent_output(text: str, language_code: str) -> str:
    """Post-process agent output to apply locale-appropriate formatting."""
    formatter = FORMATTERS.get(language_code, FORMATTERS["en"])

    def reformat_date(match: re.Match) -> str:
        try:
            dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return dt.strftime(formatter.date_format)
        except ValueError:
            return match.group(0)

    def reformat_usd(match: re.Match) -> str:
        try:
            amount = float(match.group(1).replace(",", ""))
            return format_currency(amount, formatter)
        except ValueError:
            return match.group(0)

    text = ISO_DATE_PATTERN.sub(reformat_date, text)
    if language_code != "en":
        text = USD_PATTERN.sub(reformat_usd, text)

    return text

# Usage
en_response = "The event is on 2024-03-15. The price is $1234.56."
print(f"FR: {localize_agent_output(en_response, 'fr')}")
# FR: The event is on 15/03/2024. The price is 1 234,56 €
print(f"JA: {localize_agent_output(en_response, 'ja')}")
# JA: The event is on 2024年03月15日. The price is ¥1,234.56
```

---

## Solution 5 — Language-Aware Tool Argument Handling

When the agent calls tools with user-provided inputs (search queries, addresses, dates), translate and normalize tool arguments to the expected format before execution.

```python
import asyncio
import re
from dataclasses import dataclass
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ToolArgLocalizationRule:
    tool_name: str
    arg_name: str
    normalization: str  # "date_to_iso", "translate_to_en", "normalize_currency"

LOCALIZATION_RULES: list[ToolArgLocalizationRule] = [
    ToolArgLocalizationRule("search_web", "query", "translate_to_en"),
    ToolArgLocalizationRule("get_weather", "location", "translate_to_en"),
    ToolArgLocalizationRule("parse_date", "date_string", "date_to_iso"),
]

async def translate_to_english(text: str, from_language: str) -> str:
    if from_language == "en":
        return text
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Translate this from {from_language} to English (return ONLY the translation): {text}"
        }],
    )
    return response.content[0].text.strip()

LOCALE_DATE_PATTERNS = {
    "fr": re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),  # DD/MM/YYYY
    "de": re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})"),  # DD.MM.YYYY
}

def parse_locale_date_to_iso(date_str: str, language_code: str) -> str:
    pattern = LOCALE_DATE_PATTERNS.get(language_code)
    if not pattern:
        return date_str
    match = pattern.match(date_str.strip())
    if match:
        day, month, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return date_str

async def localize_tool_args(
    tool_name: str,
    args: dict,
    language_code: str,
) -> dict:
    """Normalize tool arguments for the detected locale."""
    localized = dict(args)
    for rule in LOCALIZATION_RULES:
        if rule.tool_name != tool_name or rule.arg_name not in args:
            continue
        value = args[rule.arg_name]
        if rule.normalization == "translate_to_en":
            localized[rule.arg_name] = await translate_to_english(value, language_code)
        elif rule.normalization == "date_to_iso":
            localized[rule.arg_name] = parse_locale_date_to_iso(value, language_code)
    return localized

# Usage: French user types "Météo à Paris le 15/03/2024"
# Agent extracts: tool=get_weather, args={"location": "Paris", "date": "15/03/2024"}
# After localization: {"location": "Paris", "date": "2024-03-15"}
async def demo():
    args = {"location": "Lyon", "date": "25/12/2024"}
    normalized = await localize_tool_args("get_weather", args, "fr")
    print(f"Original: {args}")
    print(f"Normalized: {normalized}")
    # {"location": "Lyon", "date": "2024-12-25"}

asyncio.run(demo())
```

---

## Solution 6 — Multilingual Fallback Chain with Quality Gate

Try the preferred language first. If the LLM's response quality in that language is low (detected by a judge), fall back to English and offer a translation note.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

QUALITY_JUDGE_PROMPT = """\
Does this response correctly answer the user's question and is it well-formed in {language}?
User question: {question}
Response: {response}
Reply with ONLY "yes" or "no"."""

async def judge_response_quality(question: str, response: str, language: str) -> bool:
    result = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": QUALITY_JUDGE_PROMPT.format(
                language=language, question=question[:100], response=response[:200]
            )
        }],
    )
    return result.content[0].text.strip().lower() == "yes"

async def multilingual_response_with_fallback(
    user_message: str,
    language_code: str,
    language_name: str,
    base_system: str,
) -> dict:
    # Try preferred language
    preferred_system = f"{base_system}\n\nAlways respond in {language_name}."
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=preferred_system,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = response.content[0].text

    # Quality gate
    is_good = await judge_response_quality(user_message, response_text, language_name)
    if is_good:
        return {"text": response_text, "language": language_code, "fallback": False}

    # Fallback to English
    english_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"{base_system}\n\nRespond in English.",
        messages=[{"role": "user", "content": user_message}],
    )
    english_text = english_response.content[0].text

    # Translate English response to preferred language
    translated = await translate_to_english.__wrapped__ if hasattr(translate_to_english, '__wrapped__') else english_text
    # Simple: ask LLM to translate
    translation_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Translate to {language_name} (return ONLY the translation):\n{english_text}"
        }],
    )
    translated_text = translation_resp.content[0].text

    note = f"[Note: Response translated from English due to quality issues in {language_name}]\n\n"
    return {
        "text": note + translated_text,
        "language": language_code,
        "fallback": True,
        "fallback_reason": "quality_gate_failed",
    }

async def demo():
    result = await multilingual_response_with_fallback(
        user_message="Kjellberg er et norsk slektsnavn",
        language_code="no",
        language_name="Norwegian",
        base_system="You are a helpful assistant.",
    )
    print(f"Used fallback: {result['fallback']}")
    print(f"Response: {result['text'][:200]}")

asyncio.run(demo())
```

---

## Comparison

| Approach | Setup Effort | Accuracy | Persisted | Handles Formatting | Best For |
|---|---|---|---|---|---|
| Dynamic system prompt | **Low** | Good | No | No | Quick per-request localization |
| User locale profile | Low | Good | **Yes** | No | Returning users, session persistence |
| Prompt template registry | Medium | **Best** | **Yes** | No | Controlled multilingual deployments |
| Output post-processing | Low | **Best** (rule-based) | No | **Yes** | Date/number/currency normalization |
| Tool arg normalization | Medium | Good | No | **Yes** | Tool-using multilingual agents |
| Fallback chain + quality gate | High | **Best** | No | No | Low-resource language robustness |
