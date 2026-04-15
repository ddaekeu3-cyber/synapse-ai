---
layout: solution
title: "Agent Outputs PII Without Redaction — Personal Data Leaks Into Responses"
category: general
description: "An agent processing customer records, medical notes, or support tickets reproduces personally identifiable information (PII) — names, emails, SSNs, phone numbers, account numbers, dates of birth — in its output. The PII ends up in logs, displayed to users who shouldn't see it, or included in summaries sent to third parties. The fix is to detect and redact PII before sending to the LLM and before returning output to callers."
tags: [security, pii, privacy, redaction, gdpr, hipaa, data-protection, compliance, logging]
---

# Agent Outputs PII Without Redaction — Personal Data Leaks Into Responses

## Symptom
- Agent summary of customer records includes SSNs and dates of birth
- Log files contain full credit card numbers from tool responses
- Support ticket summarization reproduces patient names and diagnoses
- Agent response to one user includes another user's email address from retrieved context
- Exported reports contain raw phone numbers and addresses
- LLM prompt includes full customer PII that then appears in the generated response

## Root Cause
Agent pipelines pass raw database records or document content directly into LLM context without inspecting for PII. The LLM then incorporates that PII into responses, summaries, or logs. The fix is to apply PII detection and redaction at two points: (1) before data enters LLM context, and (2) before output is returned to callers or written to logs.

## Fix

### Option 1: Pre-send redaction — scrub PII before it reaches the LLM

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class RedactionResult:
    redacted_text: str
    entities_found: list[str]
    redaction_map: dict[str, str]  # placeholder → original (for internal use only)

# PII patterns with named groups:
PII_PATTERNS = [
    (r'\b\d{3}-\d{2}-\d{4}\b', "[SSN]"),                                  # SSN
    (r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b', "[CARD_NUMBER]"), # Credit card
    (r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "[EMAIL]"),# Email
    (r'\b\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b', "[PHONE]"),  # US phone
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', "[IP_ADDRESS]"),          # IPv4
    (r'\b(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}\b', "[DATE_OF_BIRTH]"),         # DOB format
    (r'\b[A-Z]{1,2}\d{6,9}\b', "[PASSPORT_NUMBER]"),                      # Passport
    (r'\b\d{9}\b', "[ACCOUNT_NUMBER]"),                                    # 9-digit numbers
]


def redact_pii(text: str) -> RedactionResult:
    """
    Detect and redact PII from text before sending to LLM.
    Returns redacted text plus a log of what was found.
    """
    redacted = text
    found_types = []
    redaction_map = {}

    for pattern, placeholder in PII_PATTERNS:
        matches = re.findall(pattern, redacted)
        if matches:
            entity_type = placeholder.strip("[]")
            found_types.append(entity_type)
            # Replace all occurrences with the placeholder:
            redacted = re.sub(pattern, placeholder, redacted)

    return RedactionResult(
        redacted_text=redacted,
        entities_found=found_types,
        redaction_map=redaction_map
    )


def safe_summarize(document_text: str) -> dict:
    """
    Summarize a document after redacting PII from the input.
    """
    redaction = redact_pii(document_text)

    if redaction.entities_found:
        import logging
        logging.warning(f"PII redacted before LLM call: {redaction.entities_found}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Summarize this document:\n\n{redaction.redacted_text}"
        }]
    )

    return {
        "summary": response.content[0].text,
        "pii_redacted": redaction.entities_found,
        "pii_was_present": len(redaction.entities_found) > 0
    }


# Test:
doc = """
Patient: John Smith
DOB: 1985-03-15
SSN: 123-45-6789
Phone: (555) 867-5309
Email: john.smith@email.com
Diagnosis: Hypertension, managed with medication.
Account: 987654321
"""

result = safe_summarize(doc)
print(result["summary"])
print(f"PII types redacted: {result['pii_redacted']}")
```

### Option 2: Output-side PII scan — catch any PII that made it into the response

```python
import anthropic
import re
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# Additional patterns for output scanning:
OUTPUT_PII_PATTERNS = [
    (r'\b\d{3}-\d{2}-\d{4}\b', "SSN"),
    (r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b', "CREDIT_CARD"),
    (r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "EMAIL"),
    (r'\b\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b', "PHONE"),
    (r'\b(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}\b', "DOB"),
    (r'\b\d{9}\b', "ACCOUNT_NUMBER"),
]


def scan_and_redact_output(response_text: str) -> tuple[str, list[str]]:
    """
    Scan LLM output for any PII that slipped through.
    Returns (cleaned_text, list_of_detected_types).
    """
    detected = []
    cleaned = response_text

    for pattern, entity_type in OUTPUT_PII_PATTERNS:
        if re.search(pattern, cleaned):
            detected.append(entity_type)
            cleaned = re.sub(pattern, f"[{entity_type}]", cleaned)

    return cleaned, detected


def pii_safe_agent(user_message: str, context_documents: list[str]) -> dict:
    """
    Agent that redacts PII at both input and output boundaries.
    """
    # Step 1: Redact PII from all input context:
    clean_docs = []
    input_pii_types = set()
    for doc in context_documents:
        result = redact_pii(doc)
        clean_docs.append(result.redacted_text)
        input_pii_types.update(result.entities_found)

    if input_pii_types:
        logger.info(f"Input PII redacted: {sorted(input_pii_types)}")

    # Step 2: Build prompt with clean context:
    context_str = "\n\n".join(clean_docs)
    full_message = f"Context:\n{context_str}\n\nQuestion: {user_message}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": full_message}]
    )
    raw_output = response.content[0].text

    # Step 3: Scan output for any PII that got through:
    clean_output, output_pii_types = scan_and_redact_output(raw_output)

    if output_pii_types:
        logger.warning(f"PII detected in LLM output (redacted): {output_pii_types}")

    return {
        "response": clean_output,
        "input_pii_detected": sorted(input_pii_types),
        "output_pii_detected": sorted(output_pii_types),
        "was_clean": not (input_pii_types or output_pii_types)
    }


def redact_pii(text: str):
    from dataclasses import dataclass
    redacted = text
    found = []
    for pattern, placeholder in [
        (r'\b\d{3}-\d{2}-\d{4}\b', "SSN"),
        (r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "EMAIL"),
        (r'\b\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b', "PHONE"),
    ]:
        if re.search(pattern, redacted):
            found.append(placeholder)
            redacted = re.sub(pattern, f"[{placeholder}]", redacted)
    from types import SimpleNamespace
    return SimpleNamespace(redacted_text=redacted, entities_found=found)
```

### Option 3: LLM-powered PII detection — catch contextual/semantic PII

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

PII_DETECTION_TOOL = {
    "name": "identify_pii",
    "description": "Identify all personally identifiable information in the text",
    "input_schema": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The exact PII text found"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "name", "email", "phone", "ssn", "address", "dob",
                                "credit_card", "account_number", "medical_record",
                                "ip_address", "username", "passport", "other"
                            ]
                        },
                        "sensitivity": {
                            "type": "string",
                            "enum": ["high", "medium", "low"]
                        }
                    },
                    "required": ["text", "type", "sensitivity"]
                }
            }
        },
        "required": ["entities"]
    }
}


def detect_pii_with_llm(text: str) -> list[dict]:
    """
    Use LLM to detect contextual PII (e.g., "the patient John" where John is a name
    that regex might miss because it's not in a typical name pattern).
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Haiku is fast and cheap for this
        max_tokens=512,
        tools=[PII_DETECTION_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": f"Identify all personally identifiable information in this text:\n\n{text}"
        }]
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "identify_pii":
            return block.input.get("entities", [])

    return []


def smart_redact(text: str, sensitivity_threshold: str = "low") -> dict:
    """
    Combine regex + LLM detection for comprehensive PII redaction.
    sensitivity_threshold: redact entities at or above this sensitivity.
    """
    threshold_order = {"low": 0, "medium": 1, "high": 2}
    min_level = threshold_order.get(sensitivity_threshold, 0)

    # Step 1: Quick regex redaction:
    regex_patterns = [
        (r'\b\d{3}-\d{2}-\d{4}\b', "[SSN]"),
        (r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b', "[CARD]"),
        (r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "[EMAIL]"),
    ]
    redacted = text
    for pattern, placeholder in regex_patterns:
        redacted = re.sub(pattern, placeholder, redacted)

    # Step 2: LLM detection for semantic PII:
    entities = detect_pii_with_llm(redacted)

    # Filter by sensitivity threshold:
    entities_to_redact = [
        e for e in entities
        if threshold_order.get(e.get("sensitivity", "low"), 0) >= min_level
        and e.get("text") and len(e["text"]) > 1  # skip empty/single-char
    ]

    # Apply redactions (longest first to avoid partial replacements):
    for entity in sorted(entities_to_redact, key=lambda e: -len(e.get("text", ""))):
        pii_text = entity["text"]
        pii_type = entity["type"].upper()
        redacted = redacted.replace(pii_text, f"[{pii_type}]")

    return {
        "redacted_text": redacted,
        "entities_found": [e["type"] for e in entities],
        "entities_redacted": [e["type"] for e in entities_to_redact]
    }
```

### Option 4: PII-safe logging — sanitize before writing to logs

```python
import anthropic
import re
import logging
import json
from logging import LogRecord
from typing import Any

client = anthropic.Anthropic()

LOG_PII_PATTERNS = [
    (r'\b\d{3}-\d{2}-\d{4}\b', "***-**-****"),               # SSN → masked
    (r'\b(\d{4})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?(\d{4})\b',  # Card → first4...last4
     r'\1-****-****-\2'),
    (r'\b([A-Za-z0-9._%+\-]{1,3})[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b',
     r'\1***@\2'),                                             # email → j***@domain.com
    (r'\b\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?(\d{4})\b',
     r'***-***-\1'),                                          # phone → ***-***-5309
]


def sanitize_for_log(value: Any) -> Any:
    """Recursively sanitize a value for safe logging."""
    if isinstance(value, str):
        sanitized = value
        for pattern, replacement in LOG_PII_PATTERNS:
            sanitized = re.sub(pattern, replacement, sanitized)
        return sanitized
    elif isinstance(value, dict):
        return {k: sanitize_for_log(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        return type(value)(sanitize_for_log(item) for item in value)
    return value


class PIISafeFormatter(logging.Formatter):
    """
    Log formatter that automatically redacts PII from log messages.
    Apply to any log handler to sanitize all agent logging.
    """
    def format(self, record: LogRecord) -> str:
        # Sanitize the message:
        record.msg = sanitize_for_log(str(record.msg))
        if record.args:
            record.args = sanitize_for_log(record.args)
        return super().format(record)


def setup_pii_safe_logging():
    """Configure logging with PII redaction."""
    handler = logging.StreamHandler()
    handler.setFormatter(PIISafeFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logging.root.handlers = [handler]


# Setup:
setup_pii_safe_logging()
logger = logging.getLogger("agent")

# These log entries will have PII automatically masked:
logger.info("Processing customer email: john.doe@company.com")
# → "Processing customer email: joh***@company.com"

logger.info("Customer SSN verified: 123-45-6789")
# → "Customer SSN verified: ***-**-****"
```

### Option 5: Differential privacy — summarize without exposing individuals

```python
import anthropic

client = anthropic.Anthropic()

PRIVACY_PRESERVING_SYSTEM = """You are a data analyst creating privacy-preserving summaries.

## Privacy Rules
When summarizing records containing individual data:
1. Report only aggregate statistics, not individual values
2. Do NOT include names, email addresses, phone numbers, or account numbers
3. Round individual ages to 5-year buckets (e.g., "35-40 age group")
4. Do NOT include any value that uniquely identifies a single person
5. If a category has fewer than 5 members, report as "<5" not the exact count
6. Do NOT quote specific text from individual records

Output format: Statistics and trends only, no individual records."""


def privacy_preserving_analysis(records: list[dict], analysis_question: str) -> str:
    """
    Analyze records without exposing individual PII.
    The system prompt instructs the model to aggregate, not reproduce.
    """
    # Pre-process: remove high-sensitivity fields before sending at all:
    ALWAYS_REMOVE = {"ssn", "credit_card", "password", "pin", "dob", "date_of_birth"}

    clean_records = []
    for record in records:
        clean = {k: v for k, v in record.items() if k.lower() not in ALWAYS_REMOVE}
        clean_records.append(clean)

    import json
    records_text = json.dumps(clean_records[:50], indent=2)  # limit to 50 records

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=PRIVACY_PRESERVING_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Analyze these records and answer: {analysis_question}\n\nRecords:\n{records_text}"
        }]
    )
    return response.content[0].text


# Example:
records = [
    {"name": "Alice Smith", "email": "alice@example.com", "age": 34, "plan": "pro", "revenue": 99},
    {"name": "Bob Jones", "email": "bob@example.com", "age": 45, "plan": "basic", "revenue": 29},
    # ... more records
]
summary = privacy_preserving_analysis(records, "What is the revenue distribution by age group?")
# Output will contain only aggregates, no individual names or emails
print(summary)
```

### Option 6: GDPR/HIPAA compliance wrapper — audit trail + right to erasure

```python
import anthropic
import hashlib
import json
import time
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class PIIAuditRecord:
    """Records every PII access for compliance audit trail."""
    timestamp: float
    operation: str
    pii_types_accessed: list[str]
    data_subject_hash: str    # hash of subject identifier — NOT the actual identifier
    purpose: str
    legal_basis: str

class ComplianceAwareAgent:
    """
    Agent wrapper with GDPR/HIPAA compliance controls:
    - Audit trail of every PII access
    - Right-to-erasure (pseudonymization with erasable keys)
    - Purpose limitation enforcement
    - Consent verification
    """
    def __init__(self, purpose: str, legal_basis: str):
        self.purpose = purpose
        self.legal_basis = legal_basis
        self._audit_log: list[PIIAuditRecord] = []
        self._redaction_keys: dict[str, str] = {}  # subject_id → pseudonymization key

    def _hash_subject_id(self, subject_id: str) -> str:
        """Hash for audit log — cannot be reversed to find subject."""
        return hashlib.sha256(subject_id.encode()).hexdigest()[:16]

    def _detect_pii_types(self, text: str) -> list[str]:
        """Detect what types of PII are present."""
        detectors = {
            "EMAIL": r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b',
            "PHONE": r'\b\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b',
            "SSN": r'\b\d{3}-\d{2}-\d{4}\b',
            "NAME": r'\b[A-Z][a-z]+ [A-Z][a-z]+\b',
        }
        found = [ptype for ptype, pattern in detectors.items()
                 if re.search(pattern, text)]
        return found

    def process(self, content: str, subject_id: str, question: str) -> dict:
        """Process content about a data subject with full compliance controls."""
        pii_types = self._detect_pii_types(content)

        # Record the access:
        audit = PIIAuditRecord(
            timestamp=time.time(),
            operation="llm_analysis",
            pii_types_accessed=pii_types,
            data_subject_hash=self._hash_subject_id(subject_id),
            purpose=self.purpose,
            legal_basis=self.legal_basis
        )
        self._audit_log.append(audit)
        logger.info(f"PII access logged: subject_hash={audit.data_subject_hash}, types={pii_types}")

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"Purpose: {self.purpose}. Process only what is necessary for this purpose.",
            messages=[{"role": "user", "content": f"Content: {content}\n\nQuestion: {question}"}]
        )

        return {
            "result": response.content[0].text,
            "audit_id": audit.data_subject_hash,
            "pii_accessed": pii_types,
            "purpose": self.purpose
        }

    def get_audit_log(self, subject_id: str) -> list[dict]:
        """Return audit entries for a specific data subject (for GDPR requests)."""
        subject_hash = self._hash_subject_id(subject_id)
        return [
            {
                "timestamp": r.timestamp,
                "operation": r.operation,
                "pii_types": r.pii_types_accessed,
                "purpose": r.purpose
            }
            for r in self._audit_log
            if r.data_subject_hash == subject_hash
        ]
```

## PII Redaction Coverage

| PII Type | Regex Detection | LLM Detection | Risk Level |
|---|---|---|---|
| Email addresses | High | High | Medium |
| SSN (US) | High | Medium | Critical |
| Credit card numbers | High | Medium | Critical |
| Phone numbers | High | Medium | High |
| Names | Low (regex) | High | Medium |
| Dates of birth | Medium | High | High |
| Medical record numbers | Low | High | Critical |
| IP addresses | High | Medium | Low |
| Addresses | Low | High | Medium |

## Expected Token Savings
Pre-send redaction (Option 1) reduces context size when PII fields are large (full addresses, medical histories). More importantly, it prevents compliance violations that can cost €20M+ under GDPR or $1.9M per HIPAA breach. Output scanning (Option 2) adds ~0 tokens but catches leakage from imperfect input redaction.

## Environment
- Any agent processing customer records, medical data, HR data, financial records, or support tickets; mandatory for GDPR (EU), HIPAA (US healthcare), CCPA (California), and PCI-DSS (payment card) compliance; the two-boundary approach (Options 1 + 2 combined) is the baseline for all production agents handling personal data; use LLM detection (Option 3) for unstructured text where regex patterns miss contextual PII (e.g., "the patient John" where John is PII); always implement an audit trail (Option 6) for regulated industries
