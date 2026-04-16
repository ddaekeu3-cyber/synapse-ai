---
layout: solution
title: "Agent Doesn't Implement Input Normalization Pipeline"
category: general
description: "Clean and normalize messy user inputs before sending them to the model — fixing encoding, trimming whitespace, expanding abbreviations, and detecting language — to improve output consistency."
tags: [general, normalization, input-validation, preprocessing, encoding, pipeline]
---

# Agent Doesn't Implement Input Normalization Pipeline

Raw user input is messy: mixed encoding, inconsistent whitespace, typos, abbreviations, emoji, and mixed languages. Without normalization, the model sees inconsistent inputs and produces inconsistent outputs. The same question asked with different punctuation or casing may produce different answers. An input normalization pipeline cleanses inputs before they reach the model, reducing variance and improving reliability.

## Option 1: Basic Text Normalization with Unicode Cleanup

```python
import anthropic
import unicodedata
import re

client = anthropic.Anthropic()


def normalize_basic(text: str) -> str:
    """Normalize encoding, whitespace, and punctuation."""
    # Normalize unicode to NFC form (canonical composition)
    text = unicodedata.normalize("NFC", text)

    # Replace smart quotes with straight quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')

    # Replace em/en dashes with hyphens
    text = text.replace("\u2014", " - ").replace("\u2013", " - ")

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    # Remove control characters (except newline/tab)
    text = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\n\t")

    return text


def run_agent(raw_input: str) -> str:
    normalized = normalize_basic(raw_input)
    if normalized != raw_input:
        print(f"[normalized] {repr(raw_input[:50])} -> {repr(normalized[:50])}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": normalized}],
    )
    return response.content[0].text


# Test with messy inputs
messy_inputs = [
    "What\u2019s   the   best  way  to  learn   Python?",   # Smart apostrophe + extra spaces
    "Can you explain \u201casyncio\u201d?",                    # Smart quotes
    "What\u2019s the diff between  asyncio\u2014threading?",  # Smart apostrophe + em dash
    "Hello\x00\x01 world\x02",                               # Control characters
]

for inp in messy_inputs:
    result = run_agent(inp)
    print(f"Answer: {result[:80]}\n")

# Expected Token Savings: 5-15% from whitespace removal; primary benefit is consistency, not token savings
# Environment: Python 3.11+; NFC normalization handles combining characters; extend with unidecode for ASCII-only output
```

## Option 2: Multi-Layer Normalization Pipeline with Stage Logging

```python
import anthropic
import re
import unicodedata
from typing import Callable, NamedTuple

client = anthropic.Anthropic()


class NormalizationResult(NamedTuple):
    original: str
    normalized: str
    stages_applied: list[str]
    changes: list[tuple[str, str, str]]  # (stage, before, after)


# Pipeline stages
def stage_unicode(text: str) -> tuple[str, bool]:
    normalized = unicodedata.normalize("NFC", text)
    # Replace common problematic unicode
    mapping = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                "\u2014": " - ", "\u2013": "-", "\u00a0": " ", "\u200b": ""}
    for src, dst in mapping.items():
        normalized = normalized.replace(src, dst)
    return normalized, normalized != text


def stage_whitespace(text: str) -> tuple[str, bool]:
    normalized = re.sub(r"[ \t]+", " ", text)  # Collapse horizontal whitespace
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)  # Max 2 consecutive newlines
    normalized = normalized.strip()
    return normalized, normalized != text


def stage_punctuation(text: str) -> tuple[str, bool]:
    # Fix missing space after punctuation
    normalized = re.sub(r"([.!?])([A-Z])", r"\1 \2", text)
    # Remove duplicate punctuation
    normalized = re.sub(r"[!?]{2,}", lambda m: m.group()[0], normalized)
    return normalized, normalized != text


def stage_length_limit(text: str, max_chars: int = 8000) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    truncated = text[:max_chars] + "... [truncated]"
    return truncated, True


PIPELINE: list[tuple[str, Callable[[str], tuple[str, bool]]]] = [
    ("unicode",     stage_unicode),
    ("whitespace",  stage_whitespace),
    ("punctuation", stage_punctuation),
    ("length_limit", lambda t: stage_length_limit(t, 8000)),
]


def normalize(text: str) -> NormalizationResult:
    current = text
    stages_applied = []
    changes = []

    for stage_name, stage_fn in PIPELINE:
        result, changed = stage_fn(current)
        if changed:
            changes.append((stage_name, current[:80], result[:80]))
            stages_applied.append(stage_name)
            current = result

    return NormalizationResult(
        original=text,
        normalized=current,
        stages_applied=stages_applied,
        changes=changes,
    )


def run_agent(raw_input: str) -> str:
    result = normalize(raw_input)

    if result.stages_applied:
        print(f"[pipeline] Applied stages: {result.stages_applied}")
        for stage, before, after in result.changes:
            print(f"  [{stage}] {repr(before[:40])} -> {repr(after[:40])}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": result.normalized}],
    )
    return response.content[0].text


inputs = [
    "what\u2019s the  best framework???  for  building   REST APIs",
    "I have a question.Can you help?I need to know about asyncio",
    "A" * 8100,  # Oversized input
]

for inp in inputs:
    print(f"\nInput ({len(inp)} chars): {inp[:60]}...")
    answer = run_agent(inp)
    print(f"Answer: {answer[:100]}")

# Expected Token Savings: 10-30% for verbose inputs; punctuation fixes reduce rephrasing by model
# Environment: Python 3.11+; add/remove pipeline stages in PIPELINE list; log change rates to tune which stages fire most
```

## Option 3: Language Detection and Routing

```python
import anthropic
import re
from typing import Any

client = anthropic.Anthropic()


def detect_language(text: str) -> str:
    """
    Simple heuristic language detection.
    In production, use langdetect or fasttext-langdetect.
    """
    # Check for CJK characters
    if re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text):
        if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
            return "ja"
        return "zh"

    # Check for Korean
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"

    # Check for Arabic
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"

    # Check for common Spanish/French/German markers
    spanish = len(re.findall(r"\b(el|la|los|las|un|una|de|que|es|en|por|para)\b", text.lower()))
    if spanish > 2:
        return "es"

    return "en"


def build_language_aware_system(lang: str) -> str:
    """Build system prompt based on detected language."""
    instructions = {
        "en": "You are a helpful assistant. Respond in English.",
        "zh": "You are a helpful assistant. Respond in Chinese (中文).",
        "ja": "You are a helpful assistant. Respond in Japanese (日本語).",
        "ko": "You are a helpful assistant. Respond in Korean (한국어).",
        "es": "You are a helpful assistant. Respond in Spanish (español).",
        "ar": "You are a helpful assistant. Respond in Arabic (العربية).",
    }
    return instructions.get(lang, instructions["en"])


def normalize_for_language(text: str, lang: str) -> str:
    """Language-specific normalization."""
    import unicodedata
    text = unicodedata.normalize("NFC", text).strip()

    if lang == "en":
        # English: normalize spacing around punctuation
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s([?.!,])", r"\1", text)

    return text


def run_multilingual_agent(raw_input: str) -> str:
    lang = detect_language(raw_input)
    normalized = normalize_for_language(raw_input, lang)
    system = build_language_aware_system(lang)

    print(f"[detect] language={lang}")
    if normalized != raw_input:
        print(f"[normalize] Applied {lang}-specific normalization")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": normalized}],
    )
    return response.content[0].text


test_inputs = [
    "What is the best way to learn programming?",
    "Python을 배우는 가장 좋은 방법은 무엇인가요?",
    "¿Cuál es la mejor manera de aprender a programar?",
    "Pythonを学ぶ最良の方法は何ですか？",
]

for inp in test_inputs:
    print(f"\nInput: {inp}")
    answer = run_multilingual_agent(inp)
    print(f"Answer: {answer[:150]}")

# Expected Token Savings: N/A; language routing prevents model from responding in wrong language
# Environment: Python 3.11+; replace detect_language() with langdetect pip package for accurate detection
```

## Option 4: Input Intent Classification and Sanitization

```python
import anthropic
import re
import json

client = anthropic.Anthropic()


def classify_intent(text: str) -> dict:
    """
    Classify input intent and extract structured metadata.
    Returns: {type, has_code, has_url, word_count, estimated_complexity}
    """
    lower = text.lower()

    # Detect input type
    has_code = bool(re.search(r"```|def |class |import |function |var |const |let ", text))
    has_url = bool(re.search(r"https?://\S+", text))
    has_error = any(w in lower for w in ["error", "exception", "traceback", "failed", "broken"])
    is_question = text.strip().endswith("?") or any(
        lower.startswith(w) for w in ["what", "how", "why", "when", "where", "who", "can", "could", "would", "should", "is", "are"]
    )

    # Complexity estimation
    words = text.split()
    word_count = len(words)
    complexity = "simple" if word_count < 20 else "medium" if word_count < 100 else "complex"

    return {
        "type": "debugging" if has_error else "code_review" if has_code else "question" if is_question else "general",
        "has_code": has_code,
        "has_url": has_url,
        "word_count": word_count,
        "complexity": complexity,
    }


def sanitize(text: str) -> str:
    """Remove potentially problematic content before sending to model."""
    # Remove null bytes
    text = text.replace("\x00", "")

    # Detect and redact potential secrets (basic)
    text = re.sub(r"\b[A-Za-z0-9]{32,}\b", lambda m: m.group() if " " in text[max(0, text.index(m.group())-20):text.index(m.group())] else "[REDACTED_TOKEN]", text)

    # Normalize excessive whitespace
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r" {4,}", "   ", text)

    return text.strip()


def build_intent_aware_system(intent: dict) -> str:
    """Customize system prompt based on detected intent."""
    base = "You are a helpful assistant."
    if intent["type"] == "debugging":
        return base + " Focus on identifying the root cause of errors and providing fixes."
    if intent["type"] == "code_review":
        return base + " Provide detailed code analysis with specific improvement suggestions."
    if intent["complexity"] == "complex":
        return base + " Break down your answer into clear sections with headers."
    return base


def run_intent_aware_agent(raw_input: str) -> str:
    intent = classify_intent(raw_input)
    sanitized = sanitize(raw_input)
    system = build_intent_aware_system(intent)

    print(f"[intent] type={intent['type']} complexity={intent['complexity']} "
          f"code={intent['has_code']} words={intent['word_count']}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": sanitized}],
    )
    return response.content[0].text


test_cases = [
    "What is dependency injection?",
    "I'm getting a TypeError: 'NoneType' object is not subscriptable in my Python code. Here's the traceback...",
    "```python\ndef process(items):\n    return [x*2 for x in items]\n```\nIs this code efficient?",
    "Can   you   help    me   understand    closures    in    Python?",
]

for inp in test_cases:
    print(f"\nInput: {inp[:80]}")
    answer = run_intent_aware_agent(inp)
    print(f"Answer: {answer[:120]}")

# Expected Token Savings: N/A; intent routing improves answer quality by matching system prompt to input type
# Environment: Python 3.11+; extend classify_intent() with ML classifier for higher accuracy on ambiguous inputs
```

## Option 5: Async Normalization Pipeline with Caching

```python
import asyncio
import anthropic
import hashlib
import re
import unicodedata
from typing import Any

client = anthropic.AsyncAnthropic()

# Cache for normalized inputs
_normalization_cache: dict[str, str] = {}


def cache_key(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


async def async_normalize(text: str) -> str:
    """Normalize input asynchronously, with cache."""
    key = cache_key(text)
    if key in _normalization_cache:
        return _normalization_cache[key]

    # Simulate async normalization steps (e.g., language detection API call)
    await asyncio.sleep(0)  # Yield to event loop

    result = text
    result = unicodedata.normalize("NFC", result)
    result = result.replace("\u2019", "'").replace("\u2018", "'")
    result = result.replace("\u201c", '"').replace("\u201d", '"')
    result = re.sub(r"\s+", " ", result).strip()
    result = "".join(c for c in result if unicodedata.category(c)[0] != "C" or c in "\n\t")

    _normalization_cache[key] = result
    return result


async def run_agent(raw_input: str) -> str:
    normalized = await async_normalize(raw_input)
    changed = normalized != raw_input

    if changed:
        print(f"[normalized] Changes applied to input")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": normalized}],
    )
    return response.content[0].text


async def run_batch(inputs: list[str]) -> list[str]:
    """Run multiple inputs in parallel, each with normalization."""
    tasks = [asyncio.create_task(run_agent(inp)) for inp in inputs]
    return await asyncio.gather(*tasks)


async def main() -> None:
    inputs = [
        "What\u2019s the difference  between  lists and tuples?",
        "How do you   handle  exceptions   in  Python?",
        "Can\u2019t you  explain asyncio\u2014it\u2019s confusing.",
    ]

    print("Processing batch in parallel with normalization...")
    results = await run_batch(inputs)

    for inp, result in zip(inputs, results):
        print(f"\nQ: {inp[:60]}")
        print(f"A: {result[:100]}")

    print(f"\nCache size: {len(_normalization_cache)} entries")


asyncio.run(main())

# Expected Token Savings: Cache hits avoid redundant normalization on repeated similar inputs
# Environment: Python 3.11+; use TTL-based cache eviction in production; limit cache size to prevent memory growth
```

## Option 6: Schema-Validated Structured Input with Normalization

```python
import asyncio
import anthropic
import re
import json
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class NormalizedInput:
    """Validated and normalized input ready for the model."""
    content: str
    language: str = "en"
    intent: str = "general"
    metadata: dict = field(default_factory=dict)
    normalization_applied: list[str] = field(default_factory=list)

    def to_message(self) -> dict:
        return {"role": "user", "content": self.content}

    def to_system_addendum(self) -> str:
        hints = []
        if self.language != "en":
            hints.append(f"Respond in {self.language}.")
        if self.intent == "technical":
            hints.append("Be technically precise.")
        if self.metadata.get("urgency") == "high":
            hints.append("Be concise and direct.")
        return " ".join(hints)


def normalize_and_validate(
    raw: str,
    max_length: int = 10_000,
    min_length: int = 3,
) -> NormalizedInput | None:
    """Full normalization + validation pipeline. Returns None if input is invalid."""
    import unicodedata

    applied = []

    # Validation
    if not raw or not raw.strip():
        print("[reject] Empty input")
        return None

    text = raw.strip()

    if len(text) < min_length:
        print(f"[reject] Input too short ({len(text)} chars)")
        return None

    # Stage 1: Unicode normalization
    normalized = unicodedata.normalize("NFC", text)
    for src, dst in [("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2014", " - ")]:
        normalized = normalized.replace(src, dst)
    if normalized != text:
        applied.append("unicode")
    text = normalized

    # Stage 2: Whitespace
    cleaned = re.sub(r"\s+", " ", text).strip()
    if cleaned != text:
        applied.append("whitespace")
    text = cleaned

    # Stage 3: Length limit
    if len(text) > max_length:
        text = text[:max_length] + " [truncated]"
        applied.append("truncated")

    # Stage 4: Basic intent detection
    lower = text.lower()
    intent = "technical" if any(w in lower for w in ["python", "code", "function", "error", "api", "async"]) else "general"

    # Stage 5: Language hint (simplified)
    language = "ko" if re.search(r"[\uac00-\ud7af]", text) else \
               "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

    return NormalizedInput(
        content=text,
        language=language,
        intent=intent,
        metadata={"original_length": len(raw), "final_length": len(text)},
        normalization_applied=applied,
    )


async def run_validated_agent(raw_input: str) -> str | None:
    normalized = normalize_and_validate(raw_input)
    if normalized is None:
        return None

    if normalized.normalization_applied:
        print(f"[pipeline] Stages: {normalized.normalization_applied}")

    system = "You are a helpful assistant." + (
        f" {normalized.to_system_addendum()}" if normalized.to_system_addendum() else ""
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[normalized.to_message()],
    )
    return response.content[0].text


async def main() -> None:
    test_inputs = [
        "What\u2019s the best Python web framework?",   # Smart apostrophe
        "  ",                                            # Empty — should reject
        "ab",                                            # Too short — should reject
        "Python 비동기 프로그래밍에 대해 설명해주세요",    # Korean
        "How do I handle errors in async Python code?",  # Technical English
    ]

    for inp in test_inputs:
        print(f"\nInput: {repr(inp[:60])}")
        result = await run_validated_agent(inp)
        if result:
            print(f"Answer: {result[:120]}")
        else:
            print("Rejected by validation pipeline")


asyncio.run(main())

# Expected Token Savings: Rejected inputs save 100% of their tokens; normalization reduces 5-20% on messy inputs
# Environment: Python 3.11+; NormalizedInput dataclass makes normalization auditable and testable
```

## Comparison

| Option | Normalization Scope | Language-Aware | Intent | Async | Cache | Best For |
|--------|-------------------|----------------|--------|-------|-------|----------|
| 1. Basic Text | Unicode + whitespace | No | No | No | No | Quick cleanup for simple agents |
| 2. Multi-Stage Pipeline | Unicode + space + punct + length | No | No | No | No | Comprehensive normalization with audit log |
| 3. Language Detection | Unicode + lang-specific | Yes | No | No | No | Multilingual agents |
| 4. Intent Classification | Sanitize + classify | No | Yes | No | No | Adaptive system prompt selection |
| 5. Async + Cache | Unicode + whitespace | No | No | Yes | Yes | High-throughput batch processing |
| 6. Schema Validated | Full pipeline + validate | Yes | Yes | Yes | No | Production APIs with strict input contracts |
