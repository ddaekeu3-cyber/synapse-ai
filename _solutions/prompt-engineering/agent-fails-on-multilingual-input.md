---
layout: solution
title: "Agent Fails on Multilingual Input"
category: prompt-engineering
description: "Agent ignores the user's language and always responds in English, or produces garbled mixed-language output when users write in French, Japanese, Arabic, or other languages."
tags: [prompt-engineering, multilingual, internationalisation, language-detection, user-experience]
---

## Symptom

A French-speaking user writes "Comment puis-je réinitialiser mon mot de passe?" and the agent responds in English. A Japanese user gets a mix of Japanese and English in the same sentence. An Arabic user receives a response in the wrong script direction. A Spanish-speaking user is forced to repeat their question in English to get a useful answer. The agent was only tested in English and silently fails all non-English users.

## Root Cause

Without explicit language instructions, the model defaults to the dominant training language (English) or mirrors the language of the system prompt. If the system prompt is English, the model interprets the task as "answer like the system prompt", which means English. Additionally, language detection requires reading the user input carefully — the model will do this correctly if instructed, but will default to English otherwise.

## Fix

### Option 1 — Mirror user language instruction in the system prompt

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a helpful customer support assistant.

LANGUAGE RULE (highest priority): Always respond in the same language the user wrote in.
- If the user writes in French, respond entirely in French.
- If the user writes in Japanese, respond entirely in Japanese.
- If the user writes in Arabic, respond entirely in Arabic.
- If the user writes in Spanish, respond entirely in Spanish.
- Never mix languages within a single response.
- If you cannot detect the language, respond in English.
- This rule applies even if the rest of these instructions are in English."""

def ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

multilingual_queries = [
    "How do I reset my password?",                          # English
    "Comment puis-je réinitialiser mon mot de passe?",      # French
    "パスワードをリセットするにはどうすればよいですか？",       # Japanese
    "¿Cómo puedo restablecer mi contraseña?",               # Spanish
    "كيف يمكنني إعادة تعيين كلمة المرور الخاصة بي؟",       # Arabic
    "Wie kann ich mein Passwort zurücksetzen?",              # German
]
for q in multilingual_queries:
    reply = ask(q)
    print(f"Q ({q[:40]}): {reply[:120]}\n")
```

**Expected Token Savings:** Language mirroring eliminates the need for users to re-ask in English; prevents support escalations caused by language mismatches.
**Environment:** All customer-facing agents deployed globally; mirror-language instruction is the mandatory baseline for any multilingual deployment.

---

### Option 2 — Explicit language detection before processing

```python
import json
import anthropic

client = anthropic.Anthropic()

DETECT_SYSTEM = """Detect the language of the input text.
Return JSON only: {"language": "<full language name>", "iso_code": "<ISO 639-1 code>", "confidence": 0.0-1.0}
Examples: {"language": "French", "iso_code": "fr", "confidence": 0.99}"""

def detect_language(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=DETECT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"language": "English", "iso_code": "en", "confidence": 0.5}

def build_system(lang_info: dict) -> str:
    lang  = lang_info.get("language", "English")
    code  = lang_info.get("iso_code", "en")
    return f"""You are a helpful assistant.
The user is communicating in {lang} (ISO code: {code}).
You MUST respond entirely in {lang}. Do not use any other language.
Match the formality level of the user's message."""

def ask_multilingual(user_message: str) -> str:
    lang_info = detect_language(user_message)
    print(f"  [lang] detected: {lang_info['language']} ({lang_info['iso_code']}) confidence={lang_info['confidence']:.0%}")

    system = build_system(lang_info)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

queries = [
    "What are your business hours?",
    "Quelles sont vos heures d'ouverture?",
    "Quali sono i vostri orari di apertura?",
    "Каковы ваши рабочие часы?",
]
for q in queries:
    reply = ask_multilingual(q)
    print(f"Q: {q}")
    print(f"A: {reply[:150]}\n")
```

**Expected Token Savings:** Explicit language detection adds ~30 tokens per call but enables precise language targeting; more reliable than relying on implicit mirroring for edge cases like code-switching or short queries.
**Environment:** High-traffic multilingual agents where language accuracy is critical; detection enables language-specific routing and analytics.

---

### Option 3 — User-declared language preference in session context

```python
import anthropic

client = anthropic.Anthropic()

SUPPORTED_LANGUAGES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "ja": "Japanese",
    "zh": "Chinese (Simplified)",
    "ar": "Arabic",
    "pt": "Portuguese",
    "ko": "Korean",
    "it": "Italian",
}

def build_system_for_language(lang_code: str) -> str:
    lang_name = SUPPORTED_LANGUAGES.get(lang_code, "English")
    return f"""You are a helpful assistant for a global software platform.
The user's preferred language is {lang_name} (ISO 639-1: {lang_code}).
Always respond in {lang_name} regardless of what language the user writes in.
If the user switches languages mid-conversation, continue responding in {lang_name}."""

def create_session(lang_code: str) -> dict:
    """Create a session with an explicit language preference."""
    if lang_code not in SUPPORTED_LANGUAGES:
        lang_code = "en"
    return {"lang_code": lang_code, "history": []}

def chat(session: dict, user_message: str) -> tuple[str, dict]:
    session["history"].append({"role": "user", "content": user_message})
    system = build_system_for_language(session["lang_code"])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=session["history"],
    )
    reply = response.content[0].text
    session["history"].append({"role": "assistant", "content": reply})
    return reply, session

# French session — responds in French even if user writes in English
session_fr = create_session("fr")
for msg in [
    "Comment créer un compte?",
    "What is the pricing?",          # user switches to English
    "Merci, c'est utile.",
]:
    reply, session_fr = chat(session_fr, msg)
    print(f"User: {msg}")
    print(f"Agent: {reply[:150]}\n")
```

**Expected Token Savings:** Session-level language preference is set once at login (from browser locale or user profile) and eliminates per-turn language detection costs; consistent language throughout the session.
**Environment:** Authenticated multi-user platforms where language preference is stored in the user profile; most reliable approach because it cannot be confused by code-switching.

---

### Option 4 — Multilingual fallback chain for low-confidence detection

```python
import json
import anthropic

client = anthropic.Anthropic()

DETECT_SYSTEM = """Detect all languages present in this text.
Return JSON: {"primary": "<language>", "iso_code": "<code>", "confidence": 0.0-1.0, "mixed": bool}"""

def detect_with_confidence(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=DETECT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"primary": "English", "iso_code": "en", "confidence": 0.5, "mixed": False}

def ask_with_fallback(user_message: str, user_locale: str | None = None) -> str:
    """
    Language selection priority:
    1. User locale from session (most reliable)
    2. Detected language if confidence > 0.8
    3. Prompt the user to select language
    4. Default to English
    """
    # Priority 1: session locale
    if user_locale:
        target_lang = user_locale
        print(f"  [lang] using session locale: {target_lang}")
    else:
        detection = detect_with_confidence(user_message)
        confidence = detection.get("confidence", 0)
        primary    = detection.get("primary", "English")

        if detection.get("mixed"):
            print(f"  [lang] mixed language detected — defaulting to English")
            target_lang = "English"
        elif confidence >= 0.8:
            target_lang = primary
            print(f"  [lang] detected: {primary} ({confidence:.0%})")
        elif confidence >= 0.5:
            # Medium confidence — use detected but note uncertainty
            target_lang = primary
            print(f"  [lang] low-confidence detection: {primary} ({confidence:.0%}) — using anyway")
        else:
            target_lang = "English"
            print(f"  [lang] detection failed — defaulting to English")

    system = f"You are a helpful assistant. Respond in {target_lang}."
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

test_cases = [
    ("Bonjour, comment allez-vous?",          None,     "clear French"),
    ("Hi",                                     None,     "ambiguous short text"),
    ("I love sushi! 寿司が大好きです。",          None,     "mixed language"),
    ("¿Puede ayudarme con mi cuenta?",         None,     "clear Spanish"),
    ("How do I cancel my subscription?",       "German", "English text but German locale"),
]
for msg, locale, desc in test_cases:
    print(f"[{desc}]")
    reply = ask_with_fallback(msg, user_locale=locale)
    print(f"Q: {msg[:50]}")
    print(f"A: {reply[:120]}\n")
```

**Expected Token Savings:** Fallback chain handles edge cases (short text, mixed language, low confidence) gracefully without requiring a manual language picker from users.
**Environment:** Consumer-facing agents where users have not set a language preference; fallback chain reduces language errors without adding friction.

---

### Option 5 — Language-aware response template with RTL support

```python
import anthropic

client = anthropic.Anthropic()

LANGUAGE_CONFIG = {
    "ar": {"name": "Arabic",  "rtl": True,  "greeting": "مرحباً", "sign_off": "شكراً لك"},
    "he": {"name": "Hebrew",  "rtl": True,  "greeting": "שלום",   "sign_off": "תודה"},
    "fa": {"name": "Persian", "rtl": True,  "greeting": "سلام",   "sign_off": "متشکرم"},
    "en": {"name": "English", "rtl": False, "greeting": "Hello",  "sign_off": "Thank you"},
    "fr": {"name": "French",  "rtl": False, "greeting": "Bonjour","sign_off": "Merci"},
    "ja": {"name": "Japanese","rtl": False, "greeting": "こんにちは","sign_off": "ありがとうございます"},
}

def build_language_aware_system(iso_code: str) -> str:
    cfg = LANGUAGE_CONFIG.get(iso_code, LANGUAGE_CONFIG["en"])
    rtl_note = "Note: This is a right-to-left language. Structure your response accordingly." if cfg["rtl"] else ""
    return f"""You are a helpful assistant communicating in {cfg['name']}.
{rtl_note}
- Respond entirely in {cfg['name']}.
- Use culturally appropriate formality for {cfg['name']}.
- Begin responses with a natural {cfg['name']} opener when appropriate.
- Format lists and structure in a way natural to {cfg['name']} writing conventions."""

def localised_ask(user_message: str, iso_code: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=build_language_aware_system(iso_code),
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

test_cases = [
    ("كيف أغير بريدي الإلكتروني؟", "ar"),   # Arabic — RTL
    ("どのようにメールアドレスを変更しますか？", "ja"),  # Japanese
    ("Comment changer mon email?", "fr"),      # French
    ("How do I change my email?", "en"),       # English
]
for msg, code in test_cases:
    cfg = LANGUAGE_CONFIG.get(code, {})
    print(f"[{cfg.get('name', code)}{'  RTL' if cfg.get('rtl') else ''}]")
    reply = localised_ask(msg, code)
    print(f"Q: {msg}")
    print(f"A: {reply[:180]}\n")
```

**Expected Token Savings:** Language-aware system prompts provide cultural context beyond mere translation; reduces clarification turns caused by culturally inappropriate formality or structure.
**Environment:** Global consumer products deployed in RTL-language markets (Arabic, Hebrew, Persian); RTL awareness prevents layout and formatting issues.

---

### Option 6 — Multilingual routing: specialised model calls per language family

```python
import json
import anthropic

client = anthropic.Anthropic()

# Language families with tailored system prompt instructions
LANGUAGE_FAMILIES = {
    "latin":      ["en", "fr", "es", "it", "pt", "ro"],
    "germanic":   ["de", "nl", "sv", "da", "no"],
    "cjk":        ["ja", "zh", "ko"],
    "arabic":     ["ar", "fa", "ur"],
    "cyrillic":   ["ru", "uk", "bg", "sr"],
    "indic":      ["hi", "bn", "ta", "te"],
}

FAMILY_SYSTEM_PROMPTS = {
    "latin":    "You are a helpful assistant. Respond in the same Romance or Germanic language the user wrote in. Use formal register unless the user is casual.",
    "germanic": "You are a helpful assistant. Respond in the same Germanic language the user wrote in. German responses should use formal 'Sie' unless user uses 'du'.",
    "cjk":      "You are a helpful assistant. Respond in the same CJK language the user wrote in. Japanese responses should use polite ます/です form. Korean should use 존댓말.",
    "arabic":   "You are a helpful assistant. Respond in the same Semitic language the user wrote in. Use formal Modern Standard Arabic (MSA) unless the user's dialect is clear.",
    "cyrillic": "You are a helpful assistant. Respond in the same Slavic language the user wrote in. Match the user's formality level.",
    "default":  "You are a helpful assistant. Respond in the same language the user wrote in.",
}

DETECT_SYSTEM = 'Detect language. Return JSON only: {"iso_code": "<code>"}. Short answer, no explanation.'

def detect_iso(text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        system=DETECT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw).get("iso_code", "en")
    except json.JSONDecodeError:
        return "en"

def get_family(iso_code: str) -> str:
    for family, codes in LANGUAGE_FAMILIES.items():
        if iso_code in codes:
            return family
    return "default"

def routed_ask(user_message: str) -> str:
    iso_code = detect_iso(user_message)
    family   = get_family(iso_code)
    system   = FAMILY_SYSTEM_PROMPTS[family]
    print(f"  [route] iso={iso_code!r} family={family!r}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

queries = [
    "How do I export my data?",
    "Comment exporter mes données?",
    "データをエクスポートするにはどうすればよいですか？",
    "كيف يمكنني تصدير بياناتي؟",
    "Wie exportiere ich meine Daten?",
    "Как экспортировать мои данные?",
]
for q in queries:
    reply = routed_ask(q)
    print(f"Q: {q[:50]}")
    print(f"A: {reply[:150]}\n")
```

**Expected Token Savings:** Language-family routing applies culturally appropriate formality rules (Japanese polite forms, German Sie/du) that a single generic instruction cannot capture; prevents culturally offensive responses that cause support escalations.
**Environment:** Enterprise agents deployed in Japan, Germany, or Arabic-speaking markets where register and formality rules are culturally mandatory.

---

## Comparison

| Option | Language Source | Per-call Overhead | Handles RTL | Handles Short Text | Best For |
|---|---|---|---|---|---|
| 1. Mirror instruction | Implicit (user input) | None | No | No | Baseline — always include |
| 2. Explicit detection | LLM detection | ~30 tokens | No | Partial | High-traffic agents needing analytics |
| 3. Session locale | User profile | None | No | Yes | Authenticated platforms |
| 4. Fallback chain | Detection + locale | ~30 tokens | No | Yes | Consumer agents without profiles |
| 5. Language-aware template | Explicit iso_code | None | Yes | Yes | RTL markets, culturally-sensitive agents |
| 6. Family routing | Detection + routing | ~30 tokens | Yes | Partial | Enterprise global deployments |
