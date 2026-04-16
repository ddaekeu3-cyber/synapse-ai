---
layout: solution
title: "Agent Doesn't Implement PII Detection and Anonymization"
category: security
description: "Agents that send raw user input to LLMs can inadvertently expose personal identifiable information (PII) to third-party AI APIs. PII detection identifies sensitive data before it leaves the system, anonymizes it for processing, and optionally re-identifies in the response."
tags: [security, pii, privacy, gdpr, anonymization, python]
---

## Problem

Users routinely include names, email addresses, phone numbers, SSNs, credit card numbers, and other PII in their agent interactions. Sending this data to third-party LLM APIs may violate GDPR, HIPAA, CCPA, or PCI-DSS, and creates data breach liability. PII detection intercepts sensitive data before it reaches the API and replaces it with anonymized tokens that can be de-identified in responses.

## Solutions

### Option 1: Regex-Based PII Detector with Token Replacement

```python
import anthropic
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PIIPattern:
    name: str
    pattern: re.Pattern
    replacement_prefix: str
    severity: str = "high"  # "high" | "medium" | "low"

PII_PATTERNS = [
    PIIPattern("SSN",       re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
               "SSN", severity="high"),
    PIIPattern("CREDIT_CARD", re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),
               "CC", severity="high"),
    PIIPattern("EMAIL",     re.compile(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'),
               "EMAIL", severity="medium"),
    PIIPattern("PHONE",     re.compile(r'\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b'),
               "PHONE", severity="medium"),
    PIIPattern("IP_ADDRESS", re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
               "IP", severity="low"),
    PIIPattern("NAME_PREFIX", re.compile(r'\b(Mr\.|Mrs\.|Dr\.|Ms\.)\s+[A-Z][a-z]+\b'),
               "PERSON", severity="medium"),
]

@dataclass
class AnonymizationResult:
    original: str
    anonymized: str
    replacements: dict[str, str]  # token → original value
    pii_found: list[str]

class PIIAnonymizer:
    def __init__(self):
        self._session_map: dict[str, str] = {}  # token → original

    def anonymize(self, text: str) -> AnonymizationResult:
        anonymized = text
        replacements = {}
        pii_found = []

        for pattern in PII_PATTERNS:
            matches = pattern.pattern.findall(anonymized)
            for match in matches:
                match_str = match if isinstance(match, str) else match[0]
                if not match_str:
                    continue
                # Reuse token if same value seen before
                existing = next((t for t, v in self._session_map.items()
                                  if v == match_str), None)
                if existing:
                    token = existing
                else:
                    token = f"[{pattern.replacement_prefix}_{uuid.uuid4().hex[:6].upper()}]"
                    self._session_map[token] = match_str
                replacements[token] = match_str
                anonymized = anonymized.replace(match_str, token)
                pii_found.append(pattern.name)

        return AnonymizationResult(text, anonymized, replacements, list(set(pii_found)))

    def deanonymize(self, text: str, replacements: dict[str, str]) -> str:
        """Restore original PII values in a response."""
        result = text
        for token, original in replacements.items():
            result = result.replace(token, original)
        return result

def run_with_pii_protection(client: anthropic.Anthropic, user_input: str) -> str:
    anonymizer = PIIAnonymizer()
    result = anonymizer.anonymize(user_input)

    if result.pii_found:
        print(f"[PII DETECTED] Types: {result.pii_found}")
        print(f"[ANONYMIZED]   {result.anonymized[:80]}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": result.anonymized}],
    )
    raw_response = response.content[0].text

    # Restore PII in response if needed (e.g. agent echoed back a token)
    final_response = anonymizer.deanonymize(raw_response, result.replacements)
    return final_response

if __name__ == "__main__":
    client = anthropic.Anthropic()
    test_inputs = [
        "My name is Dr. Smith and my SSN is 123-45-6789. Can you help me?",
        "Contact me at alice@example.com or call 555-867-5309.",
        "My credit card 4111 1111 1111 1111 was charged incorrectly.",
        "What is the weather like today?",  # No PII
    ]
    for text in test_inputs:
        print(f"\nInput: {text[:70]}")
        response = run_with_pii_protection(client, text)
        print(f"Response: {response[:80]}")

# Expected Token Savings: PII anonymization is free; prevents costly compliance violations
# Environment: pip install anthropic
```

### Option 2: LLM-Powered PII Classifier with Confidence Scores

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PIIEntity:
    entity_type: str      # "name", "email", "ssn", "phone", "address", "dob", "financial"
    value: str
    confidence: float     # 0.0–1.0
    start_idx: int
    end_idx: int

@dataclass
class PIIClassification:
    has_pii: bool
    entities: list[PIIEntity]
    risk_level: str       # "none" | "low" | "medium" | "high" | "critical"
    safe_to_send: bool

PII_CLASSIFIER_PROMPT = """Identify all PII (personally identifiable information) in this text.
Return JSON:
{
  "has_pii": true/false,
  "risk_level": "none|low|medium|high|critical",
  "entities": [
    {"type": "name|email|ssn|phone|address|dob|financial|other", "value": "...", "confidence": 0.0-1.0}
  ]
}

Text: {text}

Return ONLY JSON."""

def classify_pii_with_llm(client: anthropic.Anthropic, text: str) -> PIIClassification:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user",
                   "content": PII_CLASSIFIER_PROMPT.format(text=text[:500])}],
    )
    raw = response.content[0].text.strip()
    try:
        s, e = raw.find("{"), raw.rfind("}") + 1
        data = json.loads(raw[s:e])
        entities = [
            PIIEntity(
                entity_type=ent.get("type", "other"),
                value=ent.get("value", ""),
                confidence=float(ent.get("confidence", 0.5)),
                start_idx=text.find(ent.get("value", "")),
                end_idx=text.find(ent.get("value", "")) + len(ent.get("value", "")),
            )
            for ent in data.get("entities", [])
        ]
        risk = data.get("risk_level", "none")
        has_pii = data.get("has_pii", False)
        safe = risk in ("none", "low")
        return PIIClassification(has_pii=has_pii, entities=entities,
                                  risk_level=risk, safe_to_send=safe)
    except (json.JSONDecodeError, KeyError, ValueError):
        return PIIClassification(False, [], "none", True)

def mask_entities(text: str, entities: list[PIIEntity], threshold: float = 0.7) -> str:
    masked = text
    for entity in sorted(entities, key=lambda e: -e.confidence):
        if entity.confidence >= threshold and entity.value:
            replacement = f"[{entity.entity_type.upper()}_REDACTED]"
            masked = masked.replace(entity.value, replacement)
    return masked

def run_with_llm_pii(client: anthropic.Anthropic, user_input: str) -> Optional[str]:
    classification = classify_pii_with_llm(client, user_input)
    print(f"[PII SCAN] has_pii={classification.has_pii} "
          f"risk={classification.risk_level} entities={len(classification.entities)}")

    if not classification.safe_to_send:
        print(f"[BLOCKED] PII risk level '{classification.risk_level}' too high to send to LLM")
        for entity in classification.entities:
            print(f"  {entity.entity_type}: {entity.value[:20]} "
                  f"(confidence={entity.confidence:.2f})")
        return "Cannot process: contains sensitive personal information."

    safe_input = mask_entities(user_input, classification.entities)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": safe_input}],
    )
    return response.content[0].text

if __name__ == "__main__":
    client = anthropic.Anthropic()
    inputs = [
        "Help me draft an email to john.doe@company.com about the meeting.",
        "My social security number is 987-65-4321 and I need tax advice.",
        "Explain what machine learning is.",
    ]
    for text in inputs:
        print(f"\nInput: {text[:60]}")
        result = run_with_llm_pii(client, text)
        print(f"Response: {result[:80] if result else 'BLOCKED'}")

# Expected Token Savings: LLM-based PII detection catches contextual PII missed by regex
# Environment: pip install anthropic
```

### Option 3: Async PII Pipeline with Audit Trail

```python
import anthropic
import asyncio
import re
import time
import uuid
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PII_REGEX = [
    ("EMAIL",       re.compile(r'\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b')),
    ("SSN",         re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
    ("CREDIT_CARD", re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b')),
    ("PHONE",       re.compile(r'\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b')),
    ("DATE_OF_BIRTH", re.compile(r'\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(\d{4})\b')),
]

@dataclass
class PIIAuditRecord:
    request_id: str
    user_id: str
    pii_types_found: list[str]
    action_taken: str    # "sent_as_is" | "anonymized" | "blocked"
    timestamp: float = field(default_factory=time.time)

class AsyncPIIPipeline:
    def __init__(self, audit_path: str = "/tmp/pii_audit.jsonl",
                 block_on_high_risk: bool = True):
        self._audit_path = Path(audit_path)
        self._block_high_risk = block_on_high_risk
        self._token_map: dict[str, dict[str, str]] = {}  # request_id → {token: original}
        HIGH_RISK = {"SSN", "CREDIT_CARD"}
        self._high_risk_types = HIGH_RISK

    def _scan(self, text: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for name, pattern in PII_REGEX:
            matches = pattern.findall(text)
            if matches:
                found[name] = [m if isinstance(m, str) else m[0] for m in matches]
        return found

    def _anonymize(self, text: str, found: dict[str, list[str]],
                    request_id: str) -> str:
        self._token_map[request_id] = {}
        result = text
        for pii_type, values in found.items():
            for val in values:
                token = f"[{pii_type}_{uuid.uuid4().hex[:6].upper()}]"
                self._token_map[request_id][token] = val
                result = result.replace(val, token)
        return result

    def _write_audit(self, record: PIIAuditRecord) -> None:
        with self._audit_path.open("a") as f:
            f.write(json.dumps({
                "request_id": record.request_id, "user_id": record.user_id,
                "pii_types": record.pii_types_found, "action": record.action_taken,
                "timestamp": record.timestamp,
            }) + "\n")

    async def process(self, client: anthropic.AsyncAnthropic,
                       user_id: str, text: str) -> Optional[str]:
        request_id = str(uuid.uuid4())[:8]
        found = self._scan(text)
        pii_types = list(found.keys())
        high_risk = set(pii_types) & self._high_risk_types

        if high_risk and self._block_high_risk:
            print(f"[PII:BLOCK] {user_id} request_id={request_id} "
                  f"high_risk={high_risk}")
            self._write_audit(PIIAuditRecord(request_id, user_id, pii_types, "blocked"))
            return "Cannot process: request contains high-risk personal data."

        action = "sent_as_is"
        send_text = text
        if found:
            send_text = self._anonymize(text, found, request_id)
            action = "anonymized"
            print(f"[PII:ANON] {user_id} request_id={request_id} "
                  f"types={pii_types} tokens={len(self._token_map.get(request_id, {}))}")

        self._write_audit(PIIAuditRecord(request_id, user_id, pii_types, action))

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": send_text}],
        )
        return response.content[0].text

async def main():
    client = anthropic.AsyncAnthropic()
    pipeline = AsyncPIIPipeline(block_on_high_risk=True)

    requests = [
        ("user-alice", "My email is alice@test.com. Help me write a professional bio."),
        ("user-bob",   "My SSN is 555-44-3333. Need help with my tax form."),
        ("user-carol", "What are best practices for Python error handling?"),
        ("user-dave",  "Contact me at (555) 123-4567 about my account."),
    ]

    results = await asyncio.gather(*[
        pipeline.process(client, user_id, text)
        for user_id, text in requests
    ])

    for (user_id, text), result in zip(requests, results):
        status = "OK" if result and "Cannot process" not in result else "BLOCKED"
        print(f"[{status}] {user_id}: {(result or 'None')[:60]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Blocking high-risk PII prevents the API call entirely
# Environment: pip install anthropic
```

### Option 4: Tokenization with Reversible De-identification

```python
import anthropic
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PIIToken:
    token: str        # What replaces the PII in sent text
    original: str     # Original value
    pii_type: str
    deterministic: bool  # Same input → same token (for consistency)

class DeterministicTokenizer:
    """Maps PII values to consistent tokens within a session.
    Same email always gets same token, allowing LLM to reason consistently.
    """
    def __init__(self, session_secret: str = "session-salt"):
        self._secret = session_secret
        self._token_to_original: dict[str, str] = {}
        self._original_to_token: dict[str, str] = {}

    def _make_token(self, pii_type: str, value: str) -> str:
        """Deterministic: same value always maps to same token."""
        digest = hashlib.sha256(f"{self._secret}:{pii_type}:{value}".encode()).hexdigest()[:8]
        return f"<{pii_type}_{digest.upper()}>"

    def tokenize(self, pii_type: str, value: str) -> str:
        if value in self._original_to_token:
            return self._original_to_token[value]
        token = self._make_token(pii_type, value)
        self._original_to_token[value] = token
        self._token_to_original[token] = value
        return token

    def detokenize(self, text: str) -> str:
        for token, original in self._token_to_original.items():
            text = text.replace(token, original)
        return text

    @property
    def mapping(self) -> dict:
        return dict(self._token_to_original)

PATTERNS = [
    ("EMAIL",    re.compile(r'\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b')),
    ("PHONE",    re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b')),
    ("NAME",     re.compile(r'\b(Mr\.|Ms\.|Dr\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b')),
    ("POSTCODE", re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b')),
    ("SSN",      re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
]

def process_with_tokenization(client: anthropic.Anthropic, user_id: str,
                                text: str) -> str:
    tokenizer = DeterministicTokenizer(session_secret=f"session-{user_id}")
    anonymized = text

    replaced_types = []
    for pii_type, pattern in PATTERNS:
        def replacer(m, pt=pii_type):
            val = m.group()
            token = tokenizer.tokenize(pt, val)
            replaced_types.append(pt)
            return token
        anonymized = pattern.sub(replacer, anonymized)

    if replaced_types:
        print(f"[TOKENIZED] {len(set(replaced_types))} PII types → "
              f"{len(tokenizer.mapping)} unique tokens")
        print(f"  Anonymized: {anonymized[:80]}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": anonymized}],
    )
    raw = response.content[0].text

    # Restore PII in response
    restored = tokenizer.detokenize(raw)
    return restored

if __name__ == "__main__":
    client = anthropic.Anthropic()
    tests = [
        ("u001", "Please email Dr. Johnson at doctor@clinic.org about patient 555-123-4567."),
        ("u002", "Mr. Smith called from 800-555-0100. His SSN is 123-45-6789."),
        ("u001", "Follow up with Dr. Johnson again at doctor@clinic.org."),  # Same email, same token
    ]
    for user_id, text in tests:
        print(f"\n[{user_id}] Input: {text[:60]}")
        result = process_with_tokenization(client, user_id, text)
        print(f"[{user_id}] Response: {result[:80]}")

# Expected Token Savings: Deterministic tokens enable consistent multi-turn reasoning about PII
# Environment: pip install anthropic
```

### Option 5: GDPR-Compliant PII Processing with Consent Check

```python
import anthropic
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class ProcessingBasis(Enum):
    CONSENT = "consent"
    CONTRACT = "contract"
    LEGAL_OBLIGATION = "legal_obligation"
    LEGITIMATE_INTERESTS = "legitimate_interests"
    NONE = "none"

@dataclass
class UserConsent:
    user_id: str
    basis: ProcessingBasis
    categories_consented: list[str]  # e.g. ["email", "phone"]
    expires_at: Optional[float] = None

    def allows(self, pii_category: str) -> bool:
        if self.expires_at and time.time() > self.expires_at:
            return False
        return (self.basis != ProcessingBasis.NONE and
                pii_category.lower() in self.categories_consented)

GDPR_PATTERNS = {
    "email":   re.compile(r'\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
    "phone":   re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'),
    "ssn":     re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "ip":      re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
    "postcode": re.compile(r'\b\d{5}(?:-\d{4})?\b'),
}

class GDPRPIIProcessor:
    def __init__(self):
        self._consents: dict[str, UserConsent] = {}

    def register_consent(self, consent: UserConsent) -> None:
        self._consents[consent.user_id] = consent
        print(f"[GDPR] Consent registered for {consent.user_id}: "
              f"basis={consent.basis.value} categories={consent.categories_consented}")

    def process(self, client: anthropic.Anthropic, user_id: str,
                text: str) -> Optional[str]:
        consent = self._consents.get(user_id)
        if not consent:
            print(f"[GDPR] No consent record for {user_id} — blocking PII processing")
            consent = UserConsent(user_id, ProcessingBasis.NONE, [])

        anonymized = text
        blocked_categories = []

        for category, pattern in GDPR_PATTERNS.items():
            if pattern.search(text):
                if consent.allows(category):
                    print(f"[GDPR:ALLOW] {category} for {user_id}")
                else:
                    anonymized = pattern.sub(f"[{category.upper()}_GDPR_REDACTED]", anonymized)
                    blocked_categories.append(category)

        if blocked_categories:
            print(f"[GDPR:REDACT] {user_id} — redacted: {blocked_categories}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": anonymized}],
        )
        return response.content[0].text

if __name__ == "__main__":
    client = anthropic.Anthropic()
    processor = GDPRPIIProcessor()

    # User with email consent only
    processor.register_consent(UserConsent(
        user_id="user-eu-1",
        basis=ProcessingBasis.CONSENT,
        categories_consented=["email"],
        expires_at=time.time() + 3600,
    ))
    # User with full consent
    processor.register_consent(UserConsent(
        user_id="user-eu-2",
        basis=ProcessingBasis.CONTRACT,
        categories_consented=["email", "phone", "ssn", "ip", "postcode"],
    ))

    tests = [
        ("user-eu-1", "Send update to alice@gdpr.eu or call 555-123-4567."),
        ("user-eu-2", "My details: bob@test.com, SSN 999-88-7777, zip 90210."),
        ("user-eu-3", "No consent user: contact me@example.com."),  # No consent record
    ]
    for user_id, text in tests:
        print(f"\n[{user_id}] Input: {text}")
        result = processor.process(client, user_id, text)
        print(f"[{user_id}] Response: {result[:80] if result else 'None'}")

# Expected Token Savings: GDPR-compliant minimal data processing reduces privacy liability
# Environment: pip install anthropic
```

### Option 6: Streaming PII Redaction for Real-Time Output

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Generator, Optional

OUTPUT_PII_PATTERNS = [
    re.compile(r'\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),          # email
    re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),                        # SSN
    re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),  # credit card
    re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'),              # phone
]

def redact_pii_in_stream(text_stream: Generator[str, None, None],
                          window_size: int = 60) -> Generator[str, None, None]:
    """Apply PII redaction to streaming text with a sliding window buffer.
    Buffers enough text to catch PII that spans multiple chunks."""
    buffer = ""
    for chunk in text_stream:
        buffer += chunk
        # Process buffer up to (len - window_size) to avoid cutting mid-PII
        safe_len = max(0, len(buffer) - window_size)
        if safe_len > 0:
            safe_text = buffer[:safe_len]
            for pattern in OUTPUT_PII_PATTERNS:
                safe_text = pattern.sub("[REDACTED]", safe_text)
            yield safe_text
            buffer = buffer[safe_len:]

    # Flush remaining buffer
    for pattern in OUTPUT_PII_PATTERNS:
        buffer = pattern.sub("[REDACTED]", buffer)
    if buffer:
        yield buffer

def stream_with_pii_redaction(client: anthropic.Anthropic, prompt: str) -> str:
    """Stream response with real-time PII redaction."""
    full_output = []
    print("[STREAM] ", end="", flush=True)

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        redacted_stream = redact_pii_in_stream(stream.text_stream, window_size=50)
        for chunk in redacted_stream:
            print(chunk, end="", flush=True)
            full_output.append(chunk)

    print()
    return "".join(full_output)

def stream_input_safe(client: anthropic.Anthropic, user_input: str) -> str:
    """Redact PII from input before streaming, redact output too."""
    # Input redaction
    safe_input = user_input
    for pattern in OUTPUT_PII_PATTERNS:
        safe_input = pattern.sub("[REDACTED]", safe_input)

    if safe_input != user_input:
        print(f"[INPUT REDACTED] Sending anonymized: {safe_input[:60]}")

    return stream_with_pii_redaction(client, safe_input)

if __name__ == "__main__":
    client = anthropic.Anthropic()
    prompts = [
        "My friend Alice (alice@secret.com) is 25. Write her a birthday greeting.",
        "Generate a test dataset with SSN like 123-45-6789 and phone 555-100-2000.",
        "Explain the difference between IPv4 and IPv6 addresses.",
    ]
    for prompt in prompts:
        print(f"\nPrompt: {prompt[:60]}")
        result = stream_input_safe(client, prompt)
        print(f"[COMPLETE] {len(result)} chars")

# Expected Token Savings: Stream redaction adds zero latency vs. buffering full response
# Environment: pip install anthropic
```

## Comparison

| Option | Detection Method | Reversible | GDPR Support | Streaming | Best For |
|--------|-----------------|-----------|-------------|-----------|----------|
| 1. Regex + token replace | Pattern matching | Yes (session map) | Partial | No | Quick setup |
| 2. LLM classifier | AI-powered | Partial | No | No | Contextual PII |
| 3. Async + audit | Regex + async | Partial | Audit trail | No | Production |
| 4. Deterministic tokenize | Regex | Yes (deterministic) | No | No | Multi-turn agents |
| 5. GDPR consent | Regex + consent check | No | Full | No | EU compliance |
| 6. Stream redaction | Regex + window | No | No | Yes | Real-time output |
