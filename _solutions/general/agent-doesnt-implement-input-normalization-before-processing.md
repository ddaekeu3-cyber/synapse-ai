---
title: "Agent Doesn't Implement Input Normalization Before Processing"
description: "Raw user inputs arrive with inconsistent whitespace, encoding artifacts, mixed case, unicode quirks, and extraneous formatting. Without normalization, the same semantic intent produces different model behaviors, cache misses, and unpredictable output quality."
difficulty: beginner
category: general
tags: [input-normalization, preprocessing, whitespace, unicode, encoding, consistency, caching]
---

## Problem

A user pastes text from a PDF (full of weird spaces and ligatures), submits a form with trailing newlines, or copy-pastes from Word (with smart quotes and em dashes). The model receives messy input, produces inconsistent outputs, and the semantic cache gets fragmented because "What is AI?" and "What  is  AI?" are treated as different inputs. Normalization cleans inputs before they touch the model.

```python
# BAD: raw user input goes straight to the model
async def handle(user_input: str) -> str:
    return await call_model(user_input)
# "What   is  AI?" and "What is AI?" hit different cache keys
# Smart quotes from Word confuse structured prompts
# Zero-width spaces from mobile keyboards cause subtle failures
```

## Solution 1: Basic Text Normalization Pipeline

Handle the most common input artifacts: whitespace, encoding, and control characters.

```python
import asyncio
import re
import unicodedata
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def normalize_whitespace(text: str) -> str:
    """Collapse multiple spaces, normalize line endings, strip edges."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)        # multiple spaces/tabs → single space
    text = re.sub(r"\n{3,}", "\n\n", text)      # 3+ newlines → 2
    text = re.sub(r"[ \t]+\n", "\n", text)      # trailing spaces before newline
    return text.strip()

def remove_control_characters(text: str) -> str:
    """Remove invisible/control characters except common whitespace."""
    KEEP = {"\n", "\t", " "}
    result = []
    for char in text:
        cat = unicodedata.category(char)
        if cat.startswith("C") and char not in KEEP:  # Cc, Cf, Cs, Co, Cn
            continue  # drop zero-width, BOM, etc.
        result.append(char)
    return "".join(result)

def normalize_unicode(text: str) -> str:
    """Normalize to NFC (composed form) for consistent character representation."""
    return unicodedata.normalize("NFC", text)

def normalize_quotes(text: str) -> str:
    """Convert smart quotes and typographic punctuation to ASCII equivalents."""
    replacements = {
        "\u2018": "'", "\u2019": "'",  # '' → '
        "\u201c": '"', "\u201d": '"',  # "" → "
        "\u2013": "-",                  # en dash → -
        "\u2014": "--",                 # em dash → --
        "\u2026": "...",               # ellipsis → ...
        "\u00b4": "'",                 # acute accent → '
        "\ufeff": "",                  # BOM
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text

def basic_normalize(text: str) -> str:
    """Full basic normalization pipeline."""
    text = normalize_unicode(text)
    text = remove_control_characters(text)
    text = normalize_quotes(text)
    text = normalize_whitespace(text)
    return text

async def normalized_call(user_input: str) -> str:
    clean = basic_normalize(user_input)
    if clean != user_input:
        diff = f"(normalized: {len(user_input)} → {len(clean)} chars)"
    else:
        diff = "(no change)"
    print(f"[Normalize] {diff}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": clean}]
    )
    return response.content[0].text if response.content else ""

async def main():
    messy_inputs = [
        "What   is   AI?\u200b",              # extra spaces + zero-width space
        "\u201cHello\u201d  world\r\n\r\n",   # smart quotes + Windows line endings
        "Explain\u2026 machine learning",      # ellipsis unicode
        "What\u2019s the best approach?",      # smart apostrophe
    ]
    for inp in messy_inputs:
        result = await normalized_call(inp)
        print(f"  {basic_normalize(inp)!r} → {result[:80]}\n")

asyncio.run(main())
```

## Solution 2: Semantic Normalization for Cache Key Generation

Normalize specifically for cache key consistency — same question, different surface forms → same cache key.

```python
import asyncio
import re
import unicodedata
import hashlib
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def to_cache_key(text: str) -> str:
    """
    Aggressive normalization for cache key generation.
    Loses some meaning but maximizes cache hit rate.
    """
    # Unicode normalization
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Lowercase
    text = text.lower()

    # Remove punctuation except essential
    text = re.sub(r"[^\w\s]", " ", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Remove common stop words for even more aggressive dedup
    STOP_WORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will",
                  "would", "could", "should", "may", "might", "shall", "can",
                  "please", "help", "me", "tell", "explain", "what", "how",
                  "why", "when", "where", "who"}
    words = [w for w in text.split() if w not in STOP_WORDS]
    normalized = " ".join(words)

    return hashlib.sha256(normalized.encode()).hexdigest()[:16]

_cache: dict[str, str] = {}

async def cached_normalized_call(user_input: str) -> tuple[str, bool]:
    clean_input = re.sub(r"\s+", " ", user_input).strip()
    cache_key = to_cache_key(clean_input)

    if cache_key in _cache:
        return _cache[cache_key], True  # cache hit

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": clean_input}]
    )
    result = response.content[0].text if response.content else ""
    _cache[cache_key] = result
    return result, False  # cache miss

async def main():
    # These should all hit the same cache key
    variants = [
        "What is machine learning?",
        "What  is  machine  learning?",  # extra spaces
        "what is machine learning",       # lowercase, no question mark
        "WHAT IS MACHINE LEARNING?",      # uppercase
        "What is machine learning???",    # extra punctuation
    ]

    for variant in variants:
        result, hit = await cached_normalized_call(variant)
        status = "HIT" if hit else "MISS"
        print(f"[{status}] {variant!r}")
        if not hit:
            print(f"  → {result[:80]}")

    print(f"\nCache size: {len(_cache)} entries for {len(variants)} queries")

asyncio.run(main())
```

## Solution 3: Domain-Specific Normalizer

Apply different normalization rules based on the expected input domain.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable

client = AsyncAnthropic()

@dataclass
class NormalizationRule:
    name: str
    fn: Callable[[str], str]
    description: str

def strip_code_fences(text: str) -> str:
    """Remove markdown code fences for plain-text processing."""
    return re.sub(r"```[a-z]*\n?", "", text).replace("```", "").strip()

def normalize_code_input(text: str) -> str:
    """For code review: preserve indentation but clean other artifacts."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.rstrip()  # remove trailing spaces per line
        cleaned.append(line)
    return "\n".join(cleaned).strip()

def normalize_question(text: str) -> str:
    """For Q&A: clean, ensure ends with question mark if missing."""
    text = re.sub(r"\s+", " ", text).strip()
    # Add question mark if it looks like a question but lacks one
    question_starters = ("what", "why", "how", "when", "where", "who", "which", "can", "does", "is", "are")
    if text.lower().split()[0] in question_starters and not text.endswith("?"):
        text += "?"
    return text

def normalize_document(text: str) -> str:
    """For document processing: preserve structure, clean artifacts."""
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\t", "    ", text)           # tabs to spaces
    text = re.sub(r"[ ]{5,}", "    ", text)       # excessive spaces
    text = re.sub(r"\n{4,}", "\n\n\n", text)      # max 3 consecutive newlines
    text = re.sub(r"\x0c", "\n---\n", text)       # form feed → section break
    return text.strip()

DOMAIN_NORMALIZERS: dict[str, list[NormalizationRule]] = {
    "code": [
        NormalizationRule("strip_fences", strip_code_fences, "Remove markdown fences"),
        NormalizationRule("normalize_code", normalize_code_input, "Clean code whitespace"),
    ],
    "question": [
        NormalizationRule("normalize_question", normalize_question, "Normalize question format"),
    ],
    "document": [
        NormalizationRule("normalize_document", normalize_document, "Clean document structure"),
    ],
    "general": [],
}

def normalize_for_domain(text: str, domain: str) -> tuple[str, list[str]]:
    rules = DOMAIN_NORMALIZERS.get(domain, [])
    applied = []
    for rule in rules:
        new_text = rule.fn(text)
        if new_text != text:
            applied.append(rule.name)
            text = new_text
    return text, applied

async def domain_normalized_call(text: str, domain: str = "general") -> str:
    normalized, applied = normalize_for_domain(text, domain)
    if applied:
        print(f"[Domain: {domain}] Applied rules: {applied}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": normalized}]
    )
    return response.content[0].text if response.content else ""

async def main():
    examples = [
        ("```python\ndef hello():  \n    print('hi')  \n```", "code"),
        ("what is async await", "question"),
        ("Chapter 1\n\n\n\n\nIntro text\r\n\r\nMore text", "document"),
    ]
    for text, domain in examples:
        result = await domain_normalized_call(text, domain)
        print(f"Result: {result[:120]}\n")

asyncio.run(main())
```

## Solution 4: Length and Truncation Normalization

Enforce input length limits with intelligent truncation that preserves meaning.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def truncate_preserving_structure(
    text: str,
    max_chars: int = 10000,
    preserve_end_chars: int = 500
) -> tuple[str, bool]:
    """
    Truncate long input while preserving beginning and end.
    Returns (truncated_text, was_truncated).
    """
    if len(text) <= max_chars:
        return text, False

    # Keep first portion and last portion
    keep_start = max_chars - preserve_end_chars - 50  # 50 for the notice
    keep_end = preserve_end_chars

    start_portion = text[:keep_start]
    end_portion = text[-keep_end:] if keep_end > 0 else ""

    # Find clean break points (sentence/paragraph boundaries)
    break_point = max(
        start_portion.rfind("\n\n"),
        start_portion.rfind(". "),
        start_portion.rfind("? "),
    )
    if break_point > keep_start * 0.7:  # if a clean break exists in the last 30%
        start_portion = start_portion[:break_point + 1]

    notice = f"\n\n[... {len(text) - len(start_portion) - keep_end:,} chars omitted ...]\n\n"
    return start_portion + notice + end_portion, True

def normalize_length(
    text: str,
    max_chars: int = 10000,
    min_chars: int = 2
) -> tuple[str, dict]:
    stats: dict = {"original_len": len(text), "truncated": False, "too_short": False}

    if len(text) < min_chars:
        stats["too_short"] = True
        return text, stats

    result, truncated = truncate_preserving_structure(text, max_chars)
    stats["truncated"] = truncated
    stats["final_len"] = len(result)
    return result, stats

async def length_normalized_call(user_input: str, max_chars: int = 8000) -> str:
    normalized, stats = normalize_length(user_input, max_chars)

    if stats.get("too_short"):
        return "Input is too short to process meaningfully."

    if stats.get("truncated"):
        print(f"[Length] Input truncated: {stats['original_len']:,} → {stats['final_len']:,} chars")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Summarize this:\n\n{normalized}"}]
    )
    return response.content[0].text if response.content else ""

async def main():
    short_input = "Hi"
    long_input = "This is a sentence. " * 600  # ~12,000 chars

    result = await length_normalized_call(short_input)
    print(f"Short: {result[:80]}")

    result = await length_normalized_call(long_input)
    print(f"Long: {result[:200]}")

asyncio.run(main())
```

## Solution 5: Language and Encoding Detection Normalization

Detect encoding issues and non-ASCII inputs before processing.

```python
import asyncio
import unicodedata
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def detect_encoding_issues(text: str) -> list[str]:
    """Detect common encoding artifacts."""
    issues = []
    # Mojibake patterns (UTF-8 decoded as Latin-1)
    if re.search(r"[Ã¢â‚¬â„¢]", text):
        issues.append("possible_mojibake")
    # Excessive replacement characters
    if text.count("\ufffd") > 2:
        issues.append("replacement_chars")
    # Mixed script (e.g., Cyrillic mixed with Latin that looks similar)
    scripts = set()
    for char in text:
        cat = unicodedata.category(char)
        if cat.startswith("L"):
            name = unicodedata.name(char, "")
            if "LATIN" in name:
                scripts.add("latin")
            elif "CYRILLIC" in name:
                scripts.add("cyrillic")
            elif "CJK" in name:
                scripts.add("cjk")
    if len(scripts) > 1:
        issues.append(f"mixed_scripts:{'+'.join(sorted(scripts))}")
    return issues

def fix_common_encoding_artifacts(text: str) -> str:
    """Fix common encoding artifacts."""
    # Remove BOM
    text = text.lstrip("\ufeff")
    # Replace non-breaking spaces with regular spaces
    text = text.replace("\u00a0", " ")
    # Replace various dash/hyphen forms with standard hyphen
    for dash in ["\u2010", "\u2011", "\u2012", "\u2015", "\u2212"]:
        text = text.replace(dash, "-")
    # Remove replacement characters
    text = text.replace("\ufffd", "")
    return text

def detect_script(text: str) -> str:
    """Detect primary script of the text."""
    counts: dict[str, int] = {}
    for char in text:
        if unicodedata.category(char).startswith("L"):
            name = unicodedata.name(char, "UNKNOWN")
            script = name.split()[0] if name != "UNKNOWN" else "UNKNOWN"
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return "UNKNOWN"
    return max(counts, key=lambda k: counts[k])

async def encoding_safe_call(raw_input: str) -> str:
    issues = detect_encoding_issues(raw_input)
    fixed = fix_common_encoding_artifacts(raw_input)

    if issues:
        print(f"[Encoding] Issues detected: {issues}")

    script = detect_script(fixed)
    if script not in ("LATIN", "UNKNOWN"):
        print(f"[Encoding] Non-Latin script detected: {script}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": fixed}]
    )
    return response.content[0].text if response.content else ""

async def main():
    inputs = [
        "Hello\u00a0world\ufeff",           # non-breaking space + BOM
        "What\u2019s the answer?",           # smart apostrophe
        "Normal ASCII text here.",
    ]
    for inp in inputs:
        result = await encoding_safe_call(inp)
        print(f"Input: {repr(inp[:40])}")
        print(f"Result: {result[:100]}\n")

asyncio.run(main())
```

## Solution 6: Composable Normalization Pipeline with Metrics

Build a configurable pipeline of normalization steps with metrics on what was changed.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Callable

client = AsyncAnthropic()

@dataclass
class NormStep:
    name: str
    fn: Callable[[str], str]
    enabled: bool = True

@dataclass
class NormResult:
    original: str
    normalized: str
    steps_applied: list[str]
    chars_removed: int
    processing_ms: float

    @property
    def changed(self) -> bool:
        return self.original != self.normalized

import re, unicodedata

class NormalizationPipeline:
    def __init__(self, steps: list[NormStep] | None = None):
        self._steps = steps or self._default_steps()

    def _default_steps(self) -> list[NormStep]:
        return [
            NormStep("unicode_nfc", lambda t: unicodedata.normalize("NFC", t)),
            NormStep("remove_bom", lambda t: t.lstrip("\ufeff")),
            NormStep("remove_zero_width", lambda t: re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064]", "", t)),
            NormStep("normalize_line_endings", lambda t: t.replace("\r\n", "\n").replace("\r", "\n")),
            NormStep("collapse_whitespace", lambda t: re.sub(r"[ \t]+", " ", t)),
            NormStep("trim_trailing_spaces", lambda t: re.sub(r"[ \t]+\n", "\n", t)),
            NormStep("collapse_blank_lines", lambda t: re.sub(r"\n{3,}", "\n\n", t)),
            NormStep("smart_quotes", lambda t: t.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')),
            NormStep("strip", lambda t: t.strip()),
        ]

    def run(self, text: str) -> NormResult:
        start = time.perf_counter()
        current = text
        applied = []

        for step in self._steps:
            if not step.enabled:
                continue
            result = step.fn(current)
            if result != current:
                applied.append(step.name)
                current = result

        return NormResult(
            original=text,
            normalized=current,
            steps_applied=applied,
            chars_removed=len(text) - len(current),
            processing_ms=round((time.perf_counter() - start) * 1000, 2),
        )

    def disable(self, step_name: str):
        for step in self._steps:
            if step.name == step_name:
                step.enabled = False

pipeline = NormalizationPipeline()

async def pipeline_normalized_call(user_input: str) -> tuple[str, NormResult]:
    norm_result = pipeline.run(user_input)
    if norm_result.changed:
        print(f"[Pipeline] Steps: {norm_result.steps_applied}, removed {norm_result.chars_removed} chars in {norm_result.processing_ms}ms")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": norm_result.normalized}]
    )
    return (response.content[0].text if response.content else ""), norm_result

async def main():
    messy = "\ufeffWhat\u2019s\u200b the  best\r\n\r\n\r\napproach?\t  "
    result, norm = await pipeline_normalized_call(messy)
    print(f"Original: {repr(messy)}")
    print(f"Normalized: {repr(norm.normalized)}")
    print(f"Response: {result[:150]}")

asyncio.run(main())
```

## Comparison

| Approach | Scope | Cache Impact | Processing Cost | Best For |
|---|---|---|---|---|
| Basic Text Normalization | Whitespace, encoding | High | Minimal | All agents (baseline) |
| Semantic Cache Normalization | Cache key consistency | Maximum | Minimal | Cost-sensitive, high-repeat traffic |
| Domain-Specific Normalizer | Tailored per input type | Medium | Minimal | Code review, Q&A, document agents |
| Length Normalization | Token budget protection | Low | Minimal | Long-document agents |
| Encoding Detection | Multilingual robustness | Low | Low | International user bases |
| Composable Pipeline | Configurable, metrics | High | Minimal | Production with observability needs |

**Rule of thumb**: Always apply basic text normalization (whitespace + control chars) — it's zero-cost and prevents subtle failures. Add semantic normalization if you have a response cache — it can double your cache hit rate for free. Add domain-specific rules only when you observe consistent input artifacts from your actual users.
