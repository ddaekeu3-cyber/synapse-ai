---
layout: solution
title: "Agent Doesn't Implement Streaming Content Filtering"
category: streaming
description: "Agents that stream responses without content filtering can deliver harmful, off-brand, or policy-violating content token-by-token before any moderation runs. These patterns intercept and filter streaming output in real-time."
tags: [streaming, content-filtering, moderation, safety, real-time, sse]
---

# Agent Doesn't Implement Streaming Content Filtering

## The Problem

Streaming responses reach users token-by-token before the full response is available for batch moderation. If the agent begins generating harmful, confidential, or off-brand content, users see it in real-time — even if a post-hoc filter would have caught it. The damage is done before the safety check runs.

Real-time streaming content filtering intercepts the stream, buffers partial output, applies filters as content grows, and can halt or redact the stream mid-generation.

---

## Option 1: Buffered Window Filter

Accumulate a sliding window of tokens and check for forbidden patterns; halt stream on match.

```python
import anthropic
import re
from collections.abc import Generator

client = anthropic.Anthropic()

# Forbidden patterns (regex)
FORBIDDEN_PATTERNS = [
    (re.compile(r'\b(password|secret|api[_\s]?key)\s*[:=]\s*\S+', re.I), "credential_leak"),
    (re.compile(r'\b(kill|murder|harm)\s+(yourself|myself|people)', re.I), "self_harm"),
    (re.compile(r'ignore\s+(all\s+)?(previous\s+)?instructions', re.I), "prompt_injection"),
    (re.compile(r'\b(CONFIDENTIAL|TOP SECRET|INTERNAL ONLY)\b'), "confidential_marker"),
]

def check_buffer(buffer: str) -> tuple[bool, str | None]:
    """Check accumulated buffer for forbidden patterns."""
    for pattern, label in FORBIDDEN_PATTERNS:
        if pattern.search(buffer):
            return True, label
    return False, None

def filtered_stream(
    messages: list[dict],
    system_prompt: str = "",
    window_size: int = 200,
    model: str = "claude-sonnet-4-6"
) -> Generator[dict, None, None]:
    """
    Stream response with real-time content filtering.
    Yields: {"type": "token", "text": ...} or {"type": "blocked", "reason": ...}
    """
    accumulated = ""
    kwargs = {"system": system_prompt} if system_prompt else {}

    try:
        with client.messages.stream(
            model=model,
            max_tokens=1024,
            messages=messages,
            **kwargs
        ) as stream:
            for text in stream.text_stream:
                accumulated += text

                # Check rolling window of recent content
                window = accumulated[-window_size:]
                blocked, reason = check_buffer(window)

                if blocked:
                    # Redact and halt
                    yield {"type": "blocked", "reason": reason, "accumulated_so_far": accumulated[:-len(text)]}
                    return

                yield {"type": "token", "text": text}

        yield {"type": "complete", "full_text": accumulated}

    except Exception as e:
        yield {"type": "error", "message": str(e)}

# Usage
messages = [{"role": "user", "content": "Tell me about Python best practices."}]

print("Streaming with content filter:\n")
full_text = ""
for event in filtered_stream(messages, system_prompt="You are a helpful coding assistant."):
    if event["type"] == "token":
        print(event["text"], end="", flush=True)
        full_text += event["text"]
    elif event["type"] == "blocked":
        print(f"\n\n[STREAM HALTED: {event['reason']}]")
    elif event["type"] == "complete":
        print(f"\n\n[Stream complete: {len(full_text)} chars]")
    elif event["type"] == "error":
        print(f"\n[Error: {event['message']}]")

# Expected Token Savings: Pattern matching adds zero API cost; prevents costly re-generation after post-hoc rejection
# Environment: public-facing chatbots, regulated industries, content-policy-enforced platforms
```

---

## Option 2: Sentence-Boundary Moderation

Buffer until sentence boundaries, then moderate each complete sentence before releasing it to the client.

```python
import anthropic
import re
from collections.abc import AsyncGenerator
import asyncio

client = anthropic.AsyncAnthropic()

SENTENCE_ENDINGS = re.compile(r'(?<=[.!?])\s+')

async def moderate_sentence(sentence: str) -> tuple[bool, str | None]:
    """Moderate a complete sentence. Returns (allowed, reason_if_blocked)."""
    if not sentence.strip():
        return True, None

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": f"""Is this sentence appropriate for a business chatbot?
Sentence: "{sentence}"
Reply JSON only: {{"safe": true/false, "reason": "if unsafe"}}"""
        }]
    )
    try:
        import json
        result = json.loads(resp.content[0].text.strip())
        return result.get("safe", True), result.get("reason")
    except Exception:
        return True, None

async def sentence_moderated_stream(
    messages: list[dict],
    system_prompt: str = "",
    model: str = "claude-sonnet-4-6"
) -> AsyncGenerator[dict, None]:
    """
    Stream response, buffer by sentences, moderate each before forwarding.
    """
    buffer = ""
    released_sentences = []
    kwargs = {"system": system_prompt} if system_prompt else {}

    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        async for text in stream.text_stream:
            buffer += text

            # Check for sentence boundaries
            parts = SENTENCE_ENDINGS.split(buffer)

            # All parts except the last are complete sentences
            complete = parts[:-1]
            buffer = parts[-1]  # Remainder (incomplete sentence)

            for sentence in complete:
                if not sentence.strip():
                    continue

                allowed, reason = await moderate_sentence(sentence)

                if allowed:
                    released_sentences.append(sentence)
                    yield {"type": "sentence", "text": sentence + " "}
                else:
                    yield {
                        "type": "sentence_blocked",
                        "reason": reason,
                        "redacted_length": len(sentence)
                    }
                    yield {"type": "sentence", "text": "[content removed] "}

    # Release remaining buffer (incomplete sentence)
    if buffer.strip():
        allowed, reason = await moderate_sentence(buffer)
        if allowed:
            yield {"type": "sentence", "text": buffer}
        else:
            yield {"type": "sentence_blocked", "reason": reason, "redacted_length": len(buffer)}
            yield {"type": "sentence", "text": "[content removed]"}

    yield {"type": "complete", "sentences_released": len(released_sentences)}

async def demo():
    messages = [{"role": "user", "content": "Explain three benefits of unit testing."}]
    print("Sentence-moderated stream:\n")

    async for event in sentence_moderated_stream(
        messages, system_prompt="You are a software engineering assistant."
    ):
        if event["type"] == "sentence":
            print(event["text"], end="", flush=True)
        elif event["type"] == "sentence_blocked":
            print(f"\n[BLOCKED SENTENCE: {event['reason']}]", flush=True)
        elif event["type"] == "complete":
            print(f"\n\n[Complete: {event['sentences_released']} sentences released]")

asyncio.run(demo())

# Expected Token Savings: Haiku moderator runs in parallel to streaming; latency overhead ~50-100ms per sentence
# Environment: enterprise chatbots, educational platforms, regulated content pipelines
```

---

## Option 3: Topic Drift Detector

Monitor the stream for topic drift away from the allowed domain; warn or halt if detected.

```python
import anthropic
import json
from collections.abc import Generator

client = anthropic.Anthropic()

def check_topic_drift(
    accumulated_text: str,
    allowed_topics: list[str],
    check_interval: int = 150
) -> dict:
    """Check if accumulated text has drifted from allowed topics."""
    if len(accumulated_text) < check_interval:
        return {"drifted": False, "confidence": 0.0}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Does this text stay within these allowed topics?
Allowed topics: {', '.join(allowed_topics)}

Text: "{accumulated_text[-300:]}"

Reply JSON: {{"on_topic": true/false, "detected_topic": "what it's actually about", "confidence": 0.0-1.0}}"""
        }]
    )
    try:
        result = json.loads(resp.content[0].text.strip())
        return {
            "drifted": not result.get("on_topic", True),
            "detected_topic": result.get("detected_topic", "unknown"),
            "confidence": result.get("confidence", 0.5)
        }
    except json.JSONDecodeError:
        return {"drifted": False, "confidence": 0.0}

def topic_filtered_stream(
    messages: list[dict],
    allowed_topics: list[str],
    system_prompt: str = "",
    check_every_n_chars: int = 200,
    drift_confidence_threshold: float = 0.8,
    model: str = "claude-sonnet-4-6"
) -> Generator[dict, None, None]:
    """Stream with topic drift detection; halt on off-topic content."""
    accumulated = ""
    last_check_at = 0
    check_count = 0
    kwargs = {"system": system_prompt} if system_prompt else {}

    with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        for text in stream.text_stream:
            accumulated += text

            # Periodic drift check
            if len(accumulated) - last_check_at >= check_every_n_chars:
                last_check_at = len(accumulated)
                check_count += 1

                drift = check_topic_drift(accumulated, allowed_topics)

                if drift["drifted"] and drift["confidence"] >= drift_confidence_threshold:
                    yield {
                        "type": "topic_drift_halt",
                        "detected_topic": drift["detected_topic"],
                        "allowed_topics": allowed_topics,
                        "confidence": drift["confidence"],
                        "chars_before_drift": len(accumulated)
                    }
                    return

                if drift["drifted"]:
                    # Warn but continue
                    yield {
                        "type": "topic_drift_warning",
                        "detected_topic": drift["detected_topic"],
                        "confidence": drift["confidence"]
                    }

            yield {"type": "token", "text": text}

    yield {
        "type": "complete",
        "full_text": accumulated,
        "drift_checks": check_count
    }

# Usage: customer service bot that should only discuss products
messages = [{"role": "user", "content": "Tell me about your return policy and also some Python tips."}]
allowed = ["customer service", "products", "orders", "returns", "shipping"]

print("Topic-filtered stream:\n")
for event in topic_filtered_stream(
    messages, allowed_topics=allowed,
    system_prompt="You are a customer service agent."
):
    if event["type"] == "token":
        print(event["text"], end="", flush=True)
    elif event["type"] == "topic_drift_warning":
        print(f"\n[WARNING: topic drift to '{event['detected_topic']}' ({event['confidence']:.0%})]")
    elif event["type"] == "topic_drift_halt":
        print(f"\n[HALTED: drifted to '{event['detected_topic']}' ({event['confidence']:.0%})]")
    elif event["type"] == "complete":
        print(f"\n\n[Complete. Drift checks: {event['drift_checks']}]")

# Expected Token Savings: Haiku drift check every 200 chars adds ~80 tokens per check; catches topic violation early
# Environment: domain-restricted chatbots, brand safety, regulated industry agents
```

---

## Option 4: PII Redaction in Stream

Detect and redact personally identifiable information as it appears in the stream.

```python
import anthropic
import re
from collections.abc import Generator
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class PIIPattern:
    name: str
    pattern: re.Pattern
    replacement: str

PII_PATTERNS = [
    PIIPattern("email", re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), "[EMAIL]"),
    PIIPattern("phone_us", re.compile(r'\b(\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'), "[PHONE]"),
    PIIPattern("ssn", re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "[SSN]"),
    PIIPattern("credit_card", re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), "[CARD]"),
    PIIPattern("ip_address", re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), "[IP]"),
    PIIPattern("api_key_sk", re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'), "[API_KEY]"),
]

def redact_pii(text: str) -> tuple[str, list[str]]:
    """Redact PII from text. Returns (redacted_text, list of redacted types)."""
    redacted = text
    found = []
    for pii in PII_PATTERNS:
        if pii.pattern.search(redacted):
            redacted = pii.pattern.sub(pii.replacement, redacted)
            found.append(pii.name)
    return redacted, found

def pii_redacting_stream(
    messages: list[dict],
    system_prompt: str = "",
    model: str = "claude-sonnet-4-6",
    overlap_buffer: int = 50  # Chars to keep from prev chunk to catch PII spanning chunks
) -> Generator[dict, None, None]:
    """
    Stream with real-time PII redaction.
    Uses overlap buffer to catch PII that spans chunk boundaries.
    """
    pending = ""  # Buffer of unreleased text (waiting for more context)
    redaction_log: list[dict] = []
    char_position = 0
    kwargs = {"system": system_prompt} if system_prompt else {}

    with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        for text in stream.text_stream:
            pending += text

            # Only release up to (len - overlap_buffer) to handle boundary PII
            if len(pending) > overlap_buffer * 2:
                to_release = pending[:-overlap_buffer]
                pending = pending[-overlap_buffer:]

                redacted, found = redact_pii(to_release)

                if found:
                    redaction_log.append({
                        "position": char_position,
                        "types": found,
                        "original_length": len(to_release),
                        "redacted_length": len(redacted)
                    })

                char_position += len(to_release)
                yield {"type": "text", "text": redacted, "pii_redacted": found}

    # Flush remaining buffer
    if pending:
        redacted, found = redact_pii(pending)
        if found:
            redaction_log.append({"position": char_position, "types": found})
        yield {"type": "text", "text": redacted, "pii_redacted": found}

    yield {
        "type": "complete",
        "total_redactions": sum(len(r["types"]) for r in redaction_log),
        "redaction_log": redaction_log
    }

# Usage: ask agent something that might produce PII-like patterns
messages = [{
    "role": "user",
    "content": "Create a sample user profile with realistic-looking fake data including email and phone."
}]

print("PII-redacting stream:\n")
for event in pii_redacting_stream(messages):
    if event["type"] == "text":
        print(event["text"], end="", flush=True)
        if event["pii_redacted"]:
            print(f" [redacted: {event['pii_redacted']}]", flush=True)
    elif event["type"] == "complete":
        print(f"\n\n[Complete. Total PII redactions: {event['total_redactions']}]")
        if event["redaction_log"]:
            for entry in event["redaction_log"]:
                print(f"  Position {entry['position']}: {entry['types']}")

# Expected Token Savings: Regex PII redaction adds zero API cost; prevents GDPR/HIPAA violations from streaming
# Environment: healthcare, finance, HR systems, any GDPR/HIPAA-regulated deployment
```

---

## Option 5: Brand Voice Compliance Filter

Check streamed output against brand voice guidelines; rewrite non-compliant segments.

```python
import anthropic
import json
from collections.abc import Generator

client = anthropic.Anthropic()

BRAND_GUIDELINES = {
    "tone": "professional, friendly, never condescending",
    "forbidden_phrases": ["cheap", "budget", "as per my last email", "obviously", "simply"],
    "required_formality": "formal but approachable",
    "forbidden_topics": ["competitor names", "pricing speculation", "internal processes"]
}

def check_brand_compliance(text_segment: str, guidelines: dict) -> dict:
    """Check a text segment against brand voice guidelines."""
    forbidden = [p for p in guidelines.get("forbidden_phrases", []) if p.lower() in text_segment.lower()]
    if forbidden:
        return {"compliant": False, "issues": [f"Forbidden phrase: '{p}'" for p in forbidden], "severity": "high"}

    if len(text_segment) < 50:
        return {"compliant": True, "issues": []}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""Does this text comply with brand guidelines?

Brand: {json.dumps(guidelines)}
Text: "{text_segment}"

Reply JSON: {{"compliant": true/false, "issues": ["issue1"], "severity": "low/medium/high"}}"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return {"compliant": True, "issues": []}

def rewrite_for_brand(segment: str, issues: list[str], guidelines: dict) -> str:
    """Rewrite a non-compliant segment to match brand voice."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Rewrite this text to comply with brand guidelines.

Issues to fix: {issues}
Brand guidelines: {json.dumps(guidelines)}
Original: "{segment}"

Return ONLY the rewritten text, nothing else."""
        }]
    )
    return resp.content[0].text.strip()

def brand_compliant_stream(
    messages: list[dict],
    guidelines: dict,
    system_prompt: str = "",
    check_every_n_chars: int = 300,
    model: str = "claude-sonnet-4-6"
) -> Generator[dict, None, None]:
    """Stream with brand voice compliance checking and auto-rewrite."""
    buffer = ""
    released_text = ""
    last_check_at = 0
    kwargs = {"system": system_prompt} if system_prompt else {}

    with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        for text in stream.text_stream:
            buffer += text

            if len(buffer) - last_check_at >= check_every_n_chars:
                segment_to_check = buffer[last_check_at:]
                last_check_at = len(buffer)

                compliance = check_brand_compliance(segment_to_check, guidelines)

                if compliance["compliant"]:
                    released_text += segment_to_check
                    yield {"type": "text", "text": segment_to_check, "compliant": True}
                else:
                    # Rewrite non-compliant segment
                    if compliance.get("severity") in ["medium", "high"]:
                        rewritten = rewrite_for_brand(
                            segment_to_check, compliance["issues"], guidelines
                        )
                        released_text += rewritten
                        yield {
                            "type": "text",
                            "text": rewritten,
                            "compliant": True,
                            "was_rewritten": True,
                            "issues": compliance["issues"]
                        }
                    else:
                        # Low severity: pass through with warning
                        released_text += segment_to_check
                        yield {
                            "type": "text",
                            "text": segment_to_check,
                            "compliant": False,
                            "issues": compliance["issues"]
                        }

    # Release remaining
    remaining = buffer[last_check_at:]
    if remaining:
        released_text += remaining
        yield {"type": "text", "text": remaining, "compliant": True}

    yield {"type": "complete", "total_chars": len(released_text)}

# Usage
messages = [{"role": "user", "content": "Explain our refund policy to a customer."}]

print("Brand-compliant stream:\n")
rewrites = 0
for event in brand_compliant_stream(messages, BRAND_GUIDELINES):
    if event["type"] == "text":
        print(event["text"], end="", flush=True)
        if event.get("was_rewritten"):
            rewrites += 1
    elif event["type"] == "complete":
        print(f"\n\n[Complete. Rewrites: {rewrites}]")

# Expected Token Savings: Haiku compliance check + rewrite costs less than full Sonnet re-generation of entire response
# Environment: enterprise customer service, brand-critical communications, marketing bots
```

---

## Option 6: Multi-Layer Streaming Filter Pipeline

Chain multiple filters (PII, policy, topic, toxicity) as a pipeline; each filter can pass, warn, or halt.

```python
import anthropic
import re
import json
from dataclasses import dataclass, field
from collections.abc import Generator
from enum import Enum

client = anthropic.Anthropic()

class FilterDecision(str, Enum):
    PASS = "pass"
    WARN = "warn"
    HALT = "halt"
    REDACT = "redact"

@dataclass
class FilterResult:
    filter_name: str
    decision: FilterDecision
    reason: str | None = None
    redacted_text: str | None = None

@dataclass
class StreamFilter:
    name: str

    def check(self, text: str, accumulated: str) -> FilterResult:
        raise NotImplementedError

class PiiFilter(StreamFilter):
    PII_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

    def check(self, text: str, accumulated: str) -> FilterResult:
        if self.PII_RE.search(text):
            redacted = self.PII_RE.sub("[EMAIL]", text)
            return FilterResult(self.name, FilterDecision.REDACT, "PII detected", redacted)
        return FilterResult(self.name, FilterDecision.PASS)

class KeywordPolicyFilter(StreamFilter):
    def __init__(self, forbidden: list[str]):
        super().__init__("keyword_policy")
        self.forbidden = forbidden

    def check(self, text: str, accumulated: str) -> FilterResult:
        text_lower = text.lower()
        for word in self.forbidden:
            if word.lower() in text_lower:
                return FilterResult(self.name, FilterDecision.HALT, f"Forbidden keyword: '{word}'")
        return FilterResult(self.name, FilterDecision.PASS)

class LengthGuardFilter(StreamFilter):
    def __init__(self, max_chars: int = 4000):
        super().__init__("length_guard")
        self.max_chars = max_chars

    def check(self, text: str, accumulated: str) -> FilterResult:
        if len(accumulated) > self.max_chars:
            return FilterResult(self.name, FilterDecision.HALT, f"Response exceeded {self.max_chars} chars")
        return FilterResult(self.name, FilterDecision.PASS)

class ToxicityFilter(StreamFilter):
    def __init__(self, check_every_n: int = 300):
        super().__init__("toxicity")
        self.check_every_n = check_every_n
        self.last_check_at = 0

    def check(self, text: str, accumulated: str) -> FilterResult:
        if len(accumulated) - self.last_check_at < self.check_every_n:
            return FilterResult(self.name, FilterDecision.PASS)

        self.last_check_at = len(accumulated)
        window = accumulated[-self.check_every_n:]

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": f'Is this text harmful or toxic? Reply JSON: {{"toxic": true/false}}\n\nText: "{window[:200]}"'
            }]
        )
        try:
            result = json.loads(resp.content[0].text.strip())
            if result.get("toxic"):
                return FilterResult(self.name, FilterDecision.HALT, "Toxic content detected")
        except json.JSONDecodeError:
            pass
        return FilterResult(self.name, FilterDecision.PASS)

def multi_layer_filtered_stream(
    messages: list[dict],
    filters: list[StreamFilter],
    system_prompt: str = "",
    model: str = "claude-sonnet-4-6"
) -> Generator[dict, None, None]:
    """Run stream through multiple filters in pipeline order."""
    accumulated = ""
    kwargs = {"system": system_prompt} if system_prompt else {}
    filter_stats = {f.name: {"pass": 0, "warn": 0, "halt": 0, "redact": 0} for f in filters}

    with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
        **kwargs
    ) as stream:
        for raw_text in stream.text_stream:
            processed_text = raw_text
            halted = False
            filter_events = []

            # Run through filter pipeline
            for filt in filters:
                result = filt.check(processed_text, accumulated + processed_text)
                filter_stats[filt.name][result.decision.value] += 1

                if result.decision == FilterDecision.HALT:
                    halted = True
                    filter_events.append({"filter": filt.name, "decision": "halt", "reason": result.reason})
                    break
                elif result.decision == FilterDecision.REDACT and result.redacted_text:
                    processed_text = result.redacted_text
                    filter_events.append({"filter": filt.name, "decision": "redact"})
                elif result.decision == FilterDecision.WARN:
                    filter_events.append({"filter": filt.name, "decision": "warn", "reason": result.reason})

            if halted:
                yield {"type": "stream_halted", "filter_events": filter_events, "accumulated": accumulated}
                return

            accumulated += processed_text
            yield {
                "type": "text",
                "text": processed_text,
                "filter_events": filter_events if filter_events else None
            }

    yield {"type": "complete", "total_chars": len(accumulated), "filter_stats": filter_stats}

# Build pipeline
pipeline = [
    PiiFilter("pii"),
    KeywordPolicyFilter(forbidden=["confidential", "internal only", "don't tell"]),
    LengthGuardFilter(max_chars=2000),
    ToxicityFilter(check_every_n=400),
]

messages = [{"role": "user", "content": "Explain how neural networks work."}]

print("Multi-layer filtered stream:\n")
for event in multi_layer_filtered_stream(messages, pipeline):
    if event["type"] == "text":
        print(event["text"], end="", flush=True)
        if event.get("filter_events"):
            for fe in event["filter_events"]:
                print(f" [{fe['filter']}:{fe['decision']}]", end="", flush=True)
    elif event["type"] == "stream_halted":
        print(f"\n\n[STREAM HALTED by pipeline]")
        for fe in event["filter_events"]:
            print(f"  {fe['filter']}: {fe['decision']} — {fe.get('reason', '')}")
    elif event["type"] == "complete":
        print(f"\n\n[Complete: {event['total_chars']} chars]")
        print("Filter statistics:")
        for name, stats in event["filter_stats"].items():
            print(f"  {name}: {stats}")

# Expected Token Savings: Regex filters free; Haiku toxicity check every 400 chars; total overhead ~$0.001 per response
# Environment: production API with multiple safety requirements, multi-policy enforcement, enterprise compliance
```

---

## Comparison

| Option | Filter Type | Detection Method | Halt Support | Latency Impact | Best For |
|--------|------------|-----------------|--------------|---------------|----------|
| 1. Buffered Window | Pattern matching | Regex on rolling buffer | Yes | Negligible | Credential/injection detection |
| 2. Sentence Moderation | LLM moderation | Haiku per sentence | Yes | ~50-100ms/sentence | Content policy enforcement |
| 3. Topic Drift | LLM classification | Haiku every N chars | Yes (with confidence) | ~100ms/check | Domain-restricted bots |
| 4. PII Redaction | Regex replacement | Regex with overlap | No (redacts) | Negligible | GDPR/HIPAA compliance |
| 5. Brand Compliance | LLM review + rewrite | Haiku + rewrite | No (rewrites) | ~200ms/segment | Brand voice enforcement |
| 6. Multi-Layer Pipeline | Combined | Chained filters | Yes | Additive per filter | Production with multiple policies |

**Recommended defaults:**
- **GDPR/HIPAA compliance** → Option 4 (PII redaction)
- **Content policy** → Option 2 (sentence moderation)
- **Domain restriction** → Option 3 (topic drift)
- **Full production** → Option 6 (pipeline) with Option 4 (PII) always included
- **Minimal overhead** → Option 1 (buffered pattern matching)
