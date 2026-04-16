---
title: "Agent Doesn't Implement Output Content Watermarking for Attribution"
description: "AI agents generate content without embedded attribution signals, making it impossible to trace outputs back to specific agent versions, model configurations, or generation sessions — creating accountability gaps and enabling misuse of generated content."
problem_description: |
  When AI agents produce content at scale — articles, code, reports, summaries — there's no built-in mechanism to attribute that content to its source. This creates several critical problems: organizations can't audit which agent version produced a given output, bad actors can strip metadata and republish AI content as human-authored work, legal teams can't prove provenance in disputes, and quality regressions are hard to trace to specific model or prompt changes. Without watermarking, the moment an output leaves the agent, its origin is lost.
category: security
difficulty: advanced
tags: [watermarking, attribution, provenance, content-security, steganography]
---

## Solution 1: Unicode Homoglyph Watermarking

Embed invisible attribution signals by substituting visually identical Unicode characters at deterministic positions based on a secret key and session metadata.

```python
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional


# Homoglyph pairs: (standard, lookalike)
HOMOGLYPH_PAIRS = [
    ('a', '\u0430'),  # Cyrillic а
    ('e', '\u0435'),  # Cyrillic е
    ('o', '\u043e'),  # Cyrillic о
    ('p', '\u0440'),  # Cyrillic р
    ('c', '\u0441'),  # Cyrillic с
    ('x', '\u0445'),  # Cyrillic х
    ('y', '\u0443'),  # Cyrillic у
]

STANDARD_TO_VARIANT = {s: v for s, v in HOMOGLYPH_PAIRS}
VARIANT_TO_STANDARD = {v: s for s, v in HOMOGLYPH_PAIRS}


@dataclass
class WatermarkPayload:
    agent_id: str
    model_version: str
    session_id: str
    timestamp: int


class HomoglyphWatermarker:
    def __init__(self, secret_key: bytes):
        self.secret_key = secret_key

    def _payload_to_bits(self, payload: WatermarkPayload) -> list[int]:
        """Encode payload as 32-bit fingerprint."""
        combined = f"{payload.agent_id}:{payload.model_version}:{payload.session_id}:{payload.timestamp}"
        digest = hashlib.hmac_digest(self.secret_key, combined.encode(), 'sha256')
        fingerprint = struct.unpack('>I', digest[:4])[0]
        return [(fingerprint >> i) & 1 for i in range(32)]

    def _find_embeddable_positions(self, text: str) -> list[int]:
        """Find positions with substitutable characters."""
        return [i for i, ch in enumerate(text) if ch in STANDARD_TO_VARIANT]

    def embed(self, text: str, payload: WatermarkPayload) -> str:
        bits = self._payload_to_bits(payload)
        positions = self._find_embeddable_positions(text)

        if len(positions) < len(bits):
            # Not enough substitutable chars — embed as many bits as possible
            bits = bits[:len(positions)]

        chars = list(text)
        for bit, pos in zip(bits, positions):
            if bit == 1:
                chars[pos] = STANDARD_TO_VARIANT[chars[pos]]
        return ''.join(chars)

    def extract(self, text: str) -> dict:
        """Extract variant character pattern for forensic comparison."""
        positions = self._find_embeddable_positions(text)
        variant_positions = [
            i for i, ch in enumerate(text) if ch in VARIANT_TO_STANDARD
        ]

        bits_extracted = []
        for i, ch in enumerate(text):
            if ch in STANDARD_TO_VARIANT:
                bits_extracted.append(0)
            elif ch in VARIANT_TO_STANDARD:
                bits_extracted.append(1)

        return {
            "substituted_positions": variant_positions,
            "bit_pattern": bits_extracted[:32],
            "watermark_density": len(variant_positions) / max(len(text), 1),
        }

    def verify(self, text: str, payload: WatermarkPayload) -> bool:
        expected_bits = self._payload_to_bits(payload)
        extracted = self.extract(text)
        actual_bits = extracted["bit_pattern"]

        if len(actual_bits) < len(expected_bits):
            return False

        matches = sum(
            a == e for a, e in zip(actual_bits, expected_bits)
        )
        return matches / len(expected_bits) >= 0.9  # 90% match threshold


# Usage
watermarker = HomoglyphWatermarker(secret_key=b"my-secret-key-32bytes-padded!!")

payload = WatermarkPayload(
    agent_id="agent-v2.1",
    model_version="claude-sonnet-4-6",
    session_id="sess_abc123",
    timestamp=1713200000,
)

original = "The agent produces content that appears as normal text to any casual observer."
watermarked = watermarker.embed(original, payload)

print(f"Verified: {watermarker.verify(watermarked, payload)}")
print(f"Density: {watermarker.extract(watermarked)['watermark_density']:.3f}")
```

## Solution 2: Zero-Width Character Steganography

Encode binary attribution data using zero-width characters (U+200B, U+200C) inserted between words at positions derived from a keyed hash.

```python
import hashlib
import json
import base64
from typing import Any


ZWS = '\u200b'   # zero-width space  → bit 0
ZWNJ = '\u200c'  # zero-width non-joiner → bit 1
SENTINEL = '\u200d'  # zero-width joiner → byte boundary marker


class ZeroWidthWatermarker:
    def __init__(self, secret: str):
        self.secret = secret

    def _encode_payload(self, metadata: dict[str, Any]) -> str:
        """Serialize metadata to compact base64 JSON."""
        raw = json.dumps(metadata, separators=(',', ':'))
        return base64.b64encode(raw.encode()).decode()

    def _decode_payload(self, encoded: str) -> dict[str, Any]:
        raw = base64.b64decode(encoded.encode()).decode()
        return json.loads(raw)

    def _bytes_to_zwc(self, data: bytes) -> str:
        """Convert bytes to zero-width character sequence."""
        chars = []
        for byte in data:
            chars.append(SENTINEL)
            for i in range(7, -1, -1):
                chars.append(ZWS if ((byte >> i) & 1) == 0 else ZWNJ)
        return ''.join(chars)

    def _zwc_to_bytes(self, zwc: str) -> bytes:
        """Extract bytes from zero-width character sequence."""
        result = []
        i = 0
        while i < len(zwc):
            if zwc[i] == SENTINEL:
                if i + 8 < len(zwc):
                    byte_bits = zwc[i+1:i+9]
                    byte_val = 0
                    for bit_char in byte_bits:
                        byte_val = (byte_val << 1) | (0 if bit_char == ZWS else 1)
                    result.append(byte_val)
                    i += 9
                else:
                    break
            else:
                i += 1
        return bytes(result)

    def _get_insertion_index(self, words: list[str], metadata: dict) -> int:
        """Deterministically pick insertion point via HMAC."""
        key = f"{self.secret}:{metadata.get('session_id', '')}".encode()
        digest = hashlib.sha256(key).digest()
        idx = int.from_bytes(digest[:4], 'big') % max(len(words) - 1, 1)
        return idx

    def embed(self, text: str, metadata: dict[str, Any]) -> str:
        encoded = self._encode_payload(metadata)
        zwc_payload = self._bytes_to_zwc(encoded.encode())

        words = text.split(' ')
        insert_at = self._get_insertion_index(words, metadata)

        words.insert(insert_at + 1, zwc_payload)
        return ' '.join(words)

    def extract(self, text: str) -> dict[str, Any] | None:
        """Find and decode zero-width character payload."""
        # Collect all zero-width sequences
        zwc_sequence = ''.join(
            ch for ch in text
            if ch in (ZWS, ZWNJ, SENTINEL)
        )

        if not zwc_sequence:
            return None

        try:
            raw_bytes = self._zwc_to_bytes(zwc_sequence)
            encoded = raw_bytes.decode()
            return self._decode_payload(encoded)
        except Exception:
            return None


# Usage
watermarker = ZeroWidthWatermarker(secret="production-watermark-key")

metadata = {
    "agent_id": "content-agent-prod",
    "model": "claude-opus-4-6",
    "session_id": "sess_xyz789",
    "org_id": "org_acme",
    "ts": 1713200000,
}

text = "Artificial intelligence has transformed how organizations process information and make decisions."
watermarked = watermarker.embed(text, metadata)

# Text looks identical to human readers
print(f"Visual lengths differ: {len(text)} vs {len(watermarked)}")
print(f"Extracted: {watermarker.extract(watermarked)}")
```

## Solution 3: Statistical Synonym Watermarking

Encode attribution bits by selecting synonyms from pre-defined equivalence sets according to a keyed pseudo-random sequence — survives copy-paste since the word choices carry the signal.

```python
import hashlib
import re
from dataclasses import dataclass


# Synonym groups: index 0 = bit-0 word, index 1 = bit-1 word
SYNONYM_GROUPS: list[tuple[str, str]] = [
    ("use", "utilize"),
    ("start", "begin"),
    ("end", "conclude"),
    ("show", "demonstrate"),
    ("get", "obtain"),
    ("make", "create"),
    ("help", "assist"),
    ("need", "require"),
    ("find", "discover"),
    ("keep", "maintain"),
    ("allow", "enable"),
    ("check", "verify"),
    ("send", "transmit"),
    ("large", "substantial"),
    ("small", "minimal"),
    ("fast", "rapid"),
    ("hard", "difficult"),
    ("easy", "straightforward"),
]

# Build lookup: word → (group_index, bit_value)
WORD_INDEX: dict[str, tuple[int, int]] = {}
for idx, (w0, w1) in enumerate(SYNONYM_GROUPS):
    WORD_INDEX[w0.lower()] = (idx, 0)
    WORD_INDEX[w1.lower()] = (idx, 1)


@dataclass
class AttributionToken:
    agent_id: str
    run_id: str
    model: str


class SynonymWatermarker:
    def __init__(self, secret: str):
        self.secret = secret

    def _generate_bit_sequence(self, token: AttributionToken, length: int) -> list[int]:
        """Generate deterministic bit sequence from token."""
        seed = f"{self.secret}|{token.agent_id}|{token.run_id}|{token.model}"
        digest = hashlib.sha256(seed.encode()).digest()
        # Expand to needed length via repeated hashing
        bits = []
        block = digest
        while len(bits) < length:
            for byte in block:
                for i in range(7, -1, -1):
                    bits.append((byte >> i) & 1)
            block = hashlib.sha256(block).digest()
        return bits[:length]

    def embed(self, text: str, token: AttributionToken) -> str:
        words = re.findall(r'\b\w+\b|\W+', text)

        # Find positions with substitutable words
        substitutable = [
            (i, word.lower())
            for i, word in enumerate(words)
            if word.lower() in WORD_INDEX
        ]

        bits = self._generate_bit_sequence(token, len(substitutable))

        result = list(words)
        for bit, (pos, word_lower) in zip(bits, substitutable):
            group_idx, _ = WORD_INDEX[word_lower]
            target_word = SYNONYM_GROUPS[group_idx][bit]
            # Preserve original capitalization
            original = words[pos]
            if original[0].isupper():
                target_word = target_word.capitalize()
            result[pos] = target_word

        return ''.join(result)

    def extract_bits(self, text: str) -> list[tuple[int, int, int]]:
        """Extract (group_index, bit_value, position) for each watermarked word."""
        words = re.findall(r'\b\w+\b|\W+', text)
        findings = []
        for i, word in enumerate(words):
            lower = word.lower()
            if lower in WORD_INDEX:
                group_idx, bit_val = WORD_INDEX[lower]
                findings.append((group_idx, bit_val, i))
        return findings

    def verify(self, text: str, token: AttributionToken) -> tuple[bool, float]:
        findings = self.extract_bits(text)
        if not findings:
            return False, 0.0

        bits_found = [bit for _, bit, _ in findings]
        expected_bits = self._generate_bit_sequence(token, len(bits_found))

        matches = sum(a == e for a, e in zip(bits_found, expected_bits))
        confidence = matches / len(bits_found)
        return confidence >= 0.85, confidence


# Usage
watermarker = SynonymWatermarker(secret="synonym-wm-secret")

token = AttributionToken(
    agent_id="blog-agent-v3",
    run_id="run_20240416_001",
    model="claude-sonnet-4-6",
)

text = (
    "We need to find a way to help users get the information they need quickly. "
    "Large datasets require careful handling to make the system fast and easy to use."
)

watermarked = watermarker.embed(text, token)
verified, confidence = watermarker.verify(watermarked, token)

print(f"Watermarked: {watermarked}")
print(f"Verified: {verified}, Confidence: {confidence:.2%}")
```

## Solution 4: Metadata Envelope with Cryptographic Signature

Wrap agent outputs in a signed metadata envelope — stored alongside or appended to content — enabling tamper detection and full provenance chain verification.

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ProvenanceEnvelope:
    envelope_id: str
    agent_id: str
    agent_version: str
    model_id: str
    session_id: str
    org_id: str
    timestamp: float
    content_hash: str
    signature: str
    metadata: dict[str, Any]


class SignedEnvelopeWatermarker:
    ALGORITHM = "HMAC-SHA256"

    def __init__(self, signing_key: bytes):
        self.signing_key = signing_key

    def _hash_content(self, content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def _sign(self, envelope_id: str, content_hash: str,
              agent_id: str, timestamp: float) -> str:
        message = f"{envelope_id}:{content_hash}:{agent_id}:{timestamp:.6f}"
        sig = hmac.new(self.signing_key, message.encode(), hashlib.sha256)
        return sig.hexdigest()

    def wrap(
        self,
        content: str,
        agent_id: str,
        agent_version: str,
        model_id: str,
        session_id: str,
        org_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ProvenanceEnvelope:
        envelope_id = str(uuid.uuid4())
        timestamp = time.time()
        content_hash = self._hash_content(content)
        signature = self._sign(envelope_id, content_hash, agent_id, timestamp)

        return ProvenanceEnvelope(
            envelope_id=envelope_id,
            agent_id=agent_id,
            agent_version=agent_version,
            model_id=model_id,
            session_id=session_id,
            org_id=org_id,
            timestamp=timestamp,
            content_hash=content_hash,
            signature=signature,
            metadata=extra_metadata or {},
        )

    def verify_envelope(
        self, content: str, envelope: ProvenanceEnvelope
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}

        # 1. Content integrity
        actual_hash = self._hash_content(content)
        results["content_intact"] = actual_hash == envelope.content_hash

        # 2. Signature validity
        expected_sig = self._sign(
            envelope.envelope_id,
            envelope.content_hash,
            envelope.agent_id,
            envelope.timestamp,
        )
        results["signature_valid"] = hmac.compare_digest(
            expected_sig, envelope.signature
        )

        # 3. Freshness (reject envelopes older than 30 days for active checks)
        age_days = (time.time() - envelope.timestamp) / 86400
        results["age_days"] = round(age_days, 2)
        results["fresh"] = age_days <= 30

        results["overall_valid"] = (
            results["content_intact"] and results["signature_valid"]
        )
        return results

    def to_json(self, envelope: ProvenanceEnvelope) -> str:
        return json.dumps(asdict(envelope), indent=2)

    def from_json(self, data: str) -> ProvenanceEnvelope:
        return ProvenanceEnvelope(**json.loads(data))

    def append_to_content(self, content: str, envelope: ProvenanceEnvelope) -> str:
        """Append signed envelope as a hidden comment block."""
        envelope_json = self.to_json(envelope)
        return f"{content}\n\n<!--PROVENANCE:{envelope_json}:PROVENANCE-->"

    def extract_from_content(self, content_with_envelope: str) -> tuple[str, ProvenanceEnvelope | None]:
        """Extract envelope from content block."""
        import re
        pattern = r'<!--PROVENANCE:(.*?):PROVENANCE-->'
        match = re.search(pattern, content_with_envelope, re.DOTALL)
        if not match:
            return content_with_envelope, None

        envelope_json = match.group(1)
        envelope = self.from_json(envelope_json)
        clean_content = content_with_envelope[:match.start()].rstrip()
        return clean_content, envelope


# Usage
watermarker = SignedEnvelopeWatermarker(signing_key=b"32-byte-production-signing-key!!")

content = "This report analyzes Q1 performance across all business units."

envelope = watermarker.wrap(
    content=content,
    agent_id="report-agent",
    agent_version="2.4.1",
    model_id="claude-opus-4-6",
    session_id="sess_q1_2024",
    org_id="org_acme",
    extra_metadata={"report_type": "quarterly", "department": "finance"},
)

# Embed in content
content_with_wm = watermarker.append_to_content(content, envelope)

# Later: verify
extracted_content, extracted_envelope = watermarker.extract_from_content(content_with_wm)
if extracted_envelope:
    result = watermarker.verify_envelope(extracted_content, extracted_envelope)
    print(f"Verification: {result}")
```

## Solution 5: Sentence-Level Syntactic Watermarking

Encode attribution bits by choosing between syntactically equivalent sentence constructions (active/passive voice, clause ordering) according to a keyed selection rule.

```python
import hashlib
import re
from typing import Callable


@dataclass
class SyntacticRule:
    name: str
    pattern: re.Pattern
    form_0: Callable[[re.Match], str]  # bit = 0
    form_1: Callable[[re.Match], str]  # bit = 1
    confidence: float = 1.0


from dataclasses import dataclass


SYNTACTIC_RULES: list[SyntacticRule] = [
    # "X in order to Y" vs "X to Y"
    SyntacticRule(
        name="in_order_to",
        pattern=re.compile(r'\bin order to\b', re.IGNORECASE),
        form_0=lambda m: "to",
        form_1=lambda m: "in order to",
        confidence=0.9,
    ),
    # "due to the fact that" vs "because"
    SyntacticRule(
        name="because_expansion",
        pattern=re.compile(r'\bbecause\b', re.IGNORECASE),
        form_0=lambda m: "because",
        form_1=lambda m: "due to the fact that",
        confidence=0.85,
    ),
    # "at this point in time" vs "now"
    SyntacticRule(
        name="now_expansion",
        pattern=re.compile(r'\bnow\b', re.IGNORECASE),
        form_0=lambda m: "now",
        form_1=lambda m: "at this point in time",
        confidence=0.8,
    ),
    # "a number of" vs "several"
    SyntacticRule(
        name="several_expansion",
        pattern=re.compile(r'\bseveral\b', re.IGNORECASE),
        form_0=lambda m: "several",
        form_1=lambda m: "a number of",
        confidence=0.9,
    ),
    # "it is important to note that" vs "notably"
    SyntacticRule(
        name="notably_expansion",
        pattern=re.compile(r'\bnotably\b', re.IGNORECASE),
        form_0=lambda m: "notably",
        form_1=lambda m: "it is important to note that",
        confidence=0.85,
    ),
]


class SyntacticWatermarker:
    def __init__(self, secret: str):
        self.secret = secret

    def _keyed_bits(self, session_id: str, n: int) -> list[int]:
        seed = f"{self.secret}:{session_id}".encode()
        digest = hashlib.sha256(seed).digest()
        bits = []
        block = digest
        while len(bits) < n:
            for b in block:
                bits.extend([(b >> i) & 1 for i in range(7, -1, -1)])
            block = hashlib.sha256(block).digest()
        return bits[:n]

    def embed(self, text: str, session_id: str, agent_id: str) -> tuple[str, list[str]]:
        """Returns watermarked text and list of applied rule names."""
        combined_session = f"{session_id}:{agent_id}"
        applied: list[str] = []

        # Find all substitutable positions across all rules
        substitutions: list[tuple[int, int, SyntacticRule]] = []
        for rule in SYNTACTIC_RULES:
            for m in rule.pattern.finditer(text):
                substitutions.append((m.start(), m.end(), rule))

        substitutions.sort(key=lambda x: x[0])
        bits = self._keyed_bits(combined_session, len(substitutions))

        # Apply substitutions in reverse order to preserve indices
        result = text
        offset = 0
        for (start, end, rule), bit in zip(substitutions, bits):
            replacement = rule.form_1(None) if bit == 1 else rule.form_0(None)
            adj_start = start + offset
            adj_end = end + offset
            result = result[:adj_start] + replacement + result[adj_end:]
            offset += len(replacement) - (end - start)
            if bit == 1:
                applied.append(rule.name)

        return result, applied

    def detect_watermark_presence(self, text: str) -> dict[str, int]:
        """Count occurrences of expanded forms as watermark signal."""
        signals = {}
        for rule in SYNTACTIC_RULES:
            expanded_count = len(rule.pattern.findall(text))
            signals[rule.name] = expanded_count
        return signals


# Usage
watermarker = SyntacticWatermarker(secret="syntactic-wm-key")

text = (
    "Several researchers now use machine learning because it provides better results. "
    "It is notably faster to train models with modern hardware. "
    "We need to optimize because latency matters."
)

watermarked, applied_rules = watermarker.embed(text, "sess_001", "research-agent")
print(f"Watermarked: {watermarked}")
print(f"Applied rules: {applied_rules}")
print(f"Signals: {watermarker.detect_watermark_presence(watermarked)}")
```

## Solution 6: Distributed Ledger Attribution with Content Hashing

Record content fingerprints in an append-only ledger with Merkle proof support, enabling offline verification and tamper-evident audit trails.

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class LedgerEntry:
    entry_id: str
    content_hash: str
    agent_id: str
    model: str
    session_id: str
    org_id: str
    timestamp: float
    parent_hash: str  # Previous entry hash for chain integrity
    metadata: dict[str, Any] = field(default_factory=dict)
    entry_hash: str = ""  # Self-hash computed after creation

    def compute_hash(self) -> str:
        data = {
            "entry_id": self.entry_id,
            "content_hash": self.content_hash,
            "agent_id": self.agent_id,
            "model": self.model,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "parent_hash": self.parent_hash,
        }
        serialized = json.dumps(data, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()


class AttributionLedger:
    def __init__(self):
        self._entries: list[LedgerEntry] = []
        self._lock = asyncio.Lock()
        self._index: dict[str, int] = {}  # content_hash → entry position

    @property
    def head_hash(self) -> str:
        if not self._entries:
            return "0" * 64  # Genesis hash
        return self._entries[-1].entry_hash

    async def record(
        self,
        content: str,
        agent_id: str,
        model: str,
        session_id: str,
        org_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        async with self._lock:
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            entry_id = f"entry_{len(self._entries):08d}"

            entry = LedgerEntry(
                entry_id=entry_id,
                content_hash=content_hash,
                agent_id=agent_id,
                model=model,
                session_id=session_id,
                org_id=org_id,
                timestamp=time.time(),
                parent_hash=self.head_hash,
                metadata=metadata or {},
            )
            entry.entry_hash = entry.compute_hash()

            self._index[content_hash] = len(self._entries)
            self._entries.append(entry)
            return entry

    async def verify_content(self, content: str) -> dict[str, Any]:
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        if content_hash not in self._index:
            return {"found": False, "content_hash": content_hash}

        idx = self._index[content_hash]
        entry = self._entries[idx]

        # Verify entry self-hash
        expected_hash = entry.compute_hash()
        entry_valid = expected_hash == entry.entry_hash

        # Verify chain linkage
        chain_valid = True
        if idx > 0:
            prev_entry = self._entries[idx - 1]
            chain_valid = entry.parent_hash == prev_entry.entry_hash

        return {
            "found": True,
            "entry": asdict(entry),
            "entry_valid": entry_valid,
            "chain_valid": chain_valid,
            "position_in_chain": idx,
            "chain_length": len(self._entries),
        }

    def get_merkle_proof(self, content_hash: str) -> dict[str, Any]:
        """Simple inclusion proof via sibling hashes."""
        if content_hash not in self._index:
            return {"error": "not found"}

        idx = self._index[content_hash]
        proof_path = []

        i = idx
        entries = self._entries
        while i > 0:
            sibling = i - 1 if i % 2 == 1 else i + 1
            if sibling < len(entries):
                proof_path.append({
                    "position": sibling,
                    "hash": entries[sibling].entry_hash,
                    "side": "left" if sibling < i else "right",
                })
            i = (i - 1) // 2

        return {
            "leaf_hash": entries[idx].entry_hash,
            "proof_path": proof_path,
            "chain_length": len(entries),
        }

    def export_chain(self) -> list[dict]:
        return [asdict(e) for e in self._entries]


# Usage
async def main():
    ledger = AttributionLedger()

    content1 = "AI systems require careful governance to ensure responsible deployment."
    content2 = "Machine learning models should be regularly audited for bias and accuracy."

    entry1 = await ledger.record(
        content1, "governance-agent", "claude-opus-4-6",
        "sess_001", "org_acme", {"doc_type": "policy"}
    )
    entry2 = await ledger.record(
        content2, "audit-agent", "claude-sonnet-4-6",
        "sess_002", "org_acme", {"doc_type": "report"}
    )

    result = await ledger.verify_content(content1)
    print(f"Content 1 attribution: agent={result['entry']['agent_id']}, valid={result['entry_valid']}")

    proof = ledger.get_merkle_proof(hashlib.sha256(content1.encode()).hexdigest())
    print(f"Merkle proof depth: {len(proof['proof_path'])}")

    # Modified content fails verification
    modified = content1 + " (edited)"
    result2 = await ledger.verify_content(modified)
    print(f"Modified content found: {result2['found']}")

asyncio.run(main())
```

## Comparison

| Approach | Survives Copy-Paste | Survives Rephrasing | Detectable by Reader | Storage Overhead | Verification Speed | Best For |
|---|---|---|---|---|---|---|
| Unicode Homoglyphs | Yes | No | Hard | None | Fast | Short-form content attribution |
| Zero-Width Characters | Yes | No | Very Hard | Minimal | Fast | Digital documents, HTML |
| Synonym Substitution | Yes | Partial | Hard | None | Fast | Long-form text, articles |
| Signed Envelope | Yes | Yes | Medium | Low | Fast | Structured content with metadata |
| Syntactic Watermarking | Yes | Partial | Hard | None | Medium | Natural language outputs |
| Ledger Attribution | Yes | Yes | None | Medium | Fast | Audit trails, compliance |
