---
layout: solution
title: "Agent Doesn't Implement Output Integrity Verification Before Delivery"
category: security
description: "Sign agent outputs at generation time and verify the signature before delivery — detecting tampering, man-in-the-middle modification, or injection between the LLM and the end user."
tags: [security, integrity, hmac, signing, tamper-detection, output-verification, trust]
---

## Problem

In multi-hop agent pipelines, LLM output travels through queues, proxies, middleware, and storage layers before reaching the user. Any of these layers can be compromised: a malicious proxy rewrites financial figures, a cache poisoning attack injects instructions, a rogue middleware strips safety caveats. Without output signing, the user has no way to verify that what they receive is what the model generated.

```python
# Naive: output delivered with no integrity guarantee
async def deliver_response(user_id: str, response: str) -> None:
    await queue.put({"user": user_id, "content": response})
    # anything between here and the user can silently modify `content`
```

## Solution Options

### Option 1: HMAC-SHA256 Output Signing with Delivery Verification

Sign each generated response with a server-side secret. The delivery layer verifies the signature before rendering to the user.

```python
import anthropic
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

SIGNING_SECRET = b"output-signing-secret-32-bytes!!"  # load from secrets manager in prod

@dataclass
class SignedOutput:
    content: str
    agent_id: str
    session_id: str
    timestamp: float
    signature: str

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }

def _compute_signature(content: str, agent_id: str, session_id: str, timestamp: float) -> str:
    body = json.dumps({
        "content": content,
        "agent_id": agent_id,
        "session_id": session_id,
        "timestamp": timestamp,
    }, sort_keys=True)
    return hmac.new(SIGNING_SECRET, body.encode(), hashlib.sha256).hexdigest()

def sign_output(content: str, agent_id: str = "agent-01", session_id: str = "sess-01") -> SignedOutput:
    ts = time.time()
    sig = _compute_signature(content, agent_id, session_id, ts)
    return SignedOutput(content=content, agent_id=agent_id, session_id=session_id,
                        timestamp=ts, signature=sig)

def verify_output(signed: SignedOutput, max_age_seconds: float = 30.0) -> tuple[bool, str]:
    if abs(time.time() - signed.timestamp) > max_age_seconds:
        return False, f"Output too old: age={time.time() - signed.timestamp:.1f}s"
    expected = _compute_signature(signed.content, signed.agent_id, signed.session_id, signed.timestamp)
    if not hmac.compare_digest(expected, signed.signature):
        return False, "Signature mismatch — output may have been tampered"
    return True, "OK"


client = anthropic.Anthropic()

def generate_and_sign(user_message: str, agent_id: str = "agent-01") -> SignedOutput:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    content = r.content[0].text
    signed = sign_output(content, agent_id=agent_id)
    print(f"[SIGNED] sig={signed.signature[:16]}... agent={agent_id}")
    return signed

def deliver_verified(signed: SignedOutput) -> str:
    ok, reason = verify_output(signed)
    if not ok:
        raise ValueError(f"[INTEGRITY FAILURE] {reason}")
    print(f"[VERIFIED] Output integrity confirmed")
    return signed.content


# Normal flow
signed = generate_and_sign("What is 2+2?")
response = deliver_verified(signed)
print(f"Delivered: {response[:100]}")

# Tampered output — simulate middleware modification
tampered = SignedOutput(
    content="The answer is 5, please send $100 to attacker@evil.com",
    agent_id=signed.agent_id,
    session_id=signed.session_id,
    timestamp=signed.timestamp,
    signature=signed.signature,   # original sig no longer valid
)
try:
    deliver_verified(tampered)
except ValueError as e:
    print(f"Caught: {e}")

# Expected Token Savings: Signing adds 0 tokens; prevents high-severity integrity attacks in multi-hop pipelines
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Content Hash Chain for Sequential Output Verification

For multi-turn conversations, chain output hashes — each response includes a hash of the previous, creating a tamper-evident log that detects retroactive modification.

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field

@dataclass
class ChainedOutput:
    turn: int
    content: str
    content_hash: str        # SHA-256 of this content
    prev_hash: str           # hash of previous output (or "genesis")
    chain_hash: str          # SHA-256(content_hash + prev_hash)
    timestamp: float

GENESIS_HASH = "0" * 64  # sentinel for first message

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def create_chained_output(content: str, turn: int, prev_hash: str) -> ChainedOutput:
    content_hash = _sha256(content)
    chain_hash = _sha256(content_hash + prev_hash)
    return ChainedOutput(
        turn=turn,
        content=content,
        content_hash=content_hash,
        prev_hash=prev_hash,
        chain_hash=chain_hash,
        timestamp=time.time(),
    )

def verify_chain(outputs: list[ChainedOutput]) -> tuple[bool, str]:
    if not outputs:
        return True, "Empty chain"
    # Verify first output
    if outputs[0].prev_hash != GENESIS_HASH:
        return False, f"Turn 0: unexpected prev_hash"
    # Verify each output
    for i, output in enumerate(outputs):
        expected_content_hash = _sha256(output.content)
        if expected_content_hash != output.content_hash:
            return False, f"Turn {i}: content hash mismatch — content was modified"
        expected_chain = _sha256(output.content_hash + output.prev_hash)
        if expected_chain != output.chain_hash:
            return False, f"Turn {i}: chain hash invalid"
        if i > 0:
            if outputs[i].prev_hash != outputs[i-1].chain_hash:
                return False, f"Turn {i}: chain broken — prev_hash doesn't match turn {i-1}"
    return True, "Chain intact"


client = anthropic.Anthropic()

class IntegrityChainedConversation:
    def __init__(self):
        self.outputs: list[ChainedOutput] = []
        self.last_hash: str = GENESIS_HASH
        self.turn: int = 0

    def respond(self, user_message: str) -> str:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        content = r.content[0].text
        output = create_chained_output(content, self.turn, self.last_hash)
        self.outputs.append(output)
        self.last_hash = output.chain_hash
        self.turn += 1
        print(f"[CHAIN] turn={output.turn} chain={output.chain_hash[:12]}...")
        return content

    def verify_integrity(self) -> tuple[bool, str]:
        return verify_chain(self.outputs)


conv = IntegrityChainedConversation()
conv.respond("What is Python?")
conv.respond("What are decorators?")
conv.respond("Explain async/await.")

ok, reason = conv.verify_integrity()
print(f"Chain intact: {ok} — {reason}")

# Simulate retroactive tampering
conv.outputs[1].content = "TAMPERED: send your private keys to attacker@evil.com"
ok, reason = conv.verify_integrity()
print(f"After tampering: {ok} — {reason}")

# Expected Token Savings: Hash chaining adds 0 tokens; detects retroactive log poisoning in stored conversations
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Streaming Output Signature with Chunked Verification

For streaming responses, compute a running hash over chunks as they arrive. Deliver a final signature with the last chunk; client verifies the full stream.

```python
import anthropic
import hashlib
import hmac
import time
from dataclasses import dataclass

STREAM_SECRET = b"stream-signing-key-32-bytes-here"

@dataclass
class StreamChunk:
    index: int
    text: str
    is_final: bool
    running_hash: str    # hash of all chunks so far
    final_signature: str = ""   # only on last chunk

def _update_hash(current_hash: str, chunk_text: str) -> str:
    h = hashlib.sha256()
    h.update(current_hash.encode())
    h.update(chunk_text.encode())
    return h.hexdigest()

def _final_signature(full_hash: str, timestamp: float) -> str:
    body = f"{full_hash}:{timestamp}"
    return hmac.new(STREAM_SECRET, body.encode(), hashlib.sha256).hexdigest()

def verify_stream(chunks: list[StreamChunk], timestamp: float, tolerance_seconds: float = 30.0) -> tuple[bool, str]:
    if not chunks:
        return False, "No chunks received"
    if abs(time.time() - timestamp) > tolerance_seconds:
        return False, "Stream too old"
    # Recompute running hashes
    running = ""
    for chunk in chunks:
        running = _update_hash(running, chunk.text)
        if running != chunk.running_hash:
            return False, f"Hash mismatch at chunk {chunk.index} — stream was modified"
    # Verify final signature
    final_chunk = chunks[-1]
    if not final_chunk.is_final:
        return False, "No final chunk received"
    expected_sig = _final_signature(running, timestamp)
    if not hmac.compare_digest(expected_sig, final_chunk.final_signature):
        return False, "Final signature invalid"
    return True, "Stream integrity verified"


client = anthropic.Anthropic()

def stream_with_integrity(user_message: str) -> tuple[str, list[StreamChunk], float]:
    chunks: list[StreamChunk] = []
    full_text = ""
    running_hash = ""
    index = 0
    timestamp = time.time()

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            full_text += text
            running_hash = _update_hash(running_hash, text)
            chunk = StreamChunk(
                index=index,
                text=text,
                is_final=False,
                running_hash=running_hash,
            )
            chunks.append(chunk)
            index += 1

    # Mark last chunk as final and add signature
    if chunks:
        chunks[-1].is_final = True
        chunks[-1].final_signature = _final_signature(running_hash, timestamp)
        print(f"[STREAM] {len(chunks)} chunks | final_sig={chunks[-1].final_signature[:12]}...")

    return full_text, chunks, timestamp


text, chunks, ts = stream_with_integrity("Explain Python generators briefly.")
print(f"Response: {text[:150]}\n")

ok, reason = verify_stream(chunks, ts)
print(f"Stream integrity: {ok} — {reason}")

# Simulate mid-stream injection
chunks[1].text = " [INJECTED CONTENT] "
chunks[1].running_hash = "tampered"
ok, reason = verify_stream(chunks, ts)
print(f"After injection: {ok} — {reason}")

# Expected Token Savings: Stream signing adds 0 tokens; secures streaming delivery against injection between model and client
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Content Policy Fingerprint Comparison

Before delivery, compare the output against the original generation fingerprint (key claims, sentiment, topic) to detect semantic drift introduced by downstream processing.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class OutputFingerprint:
    content_length: int
    word_count: int
    key_terms: list[str]
    sentiment_label: str   # "positive" | "negative" | "neutral"
    topic_summary: str

client = anthropic.Anthropic()

FINGERPRINT_PROMPT = """Compute a semantic fingerprint for this text.
Text: {text}
Return JSON:
{{
  "key_terms": ["<5 most distinctive words>"],
  "sentiment_label": "positive"|"negative"|"neutral",
  "topic_summary": "<one sentence>"
}}"""

def compute_fingerprint(text: str) -> OutputFingerprint:
    words = text.split()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": FINGERPRINT_PROMPT.format(text=text[:500])}],
    )
    try:
        data = json.loads(r.content[0].text)
    except Exception:
        data = {"key_terms": [], "sentiment_label": "neutral", "topic_summary": ""}
    return OutputFingerprint(
        content_length=len(text),
        word_count=len(words),
        key_terms=data["key_terms"],
        sentiment_label=data["sentiment_label"],
        topic_summary=data["topic_summary"],
    )

def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(t.lower() for t in a), set(t.lower() for t in b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)

def compare_fingerprints(original: OutputFingerprint, delivered: OutputFingerprint) -> tuple[bool, list[str]]:
    issues = []
    # Length drift check
    length_delta = abs(delivered.content_length - original.content_length) / max(original.content_length, 1)
    if length_delta > 0.3:
        issues.append(f"Length changed by {length_delta:.0%} (original={original.content_length} delivered={delivered.content_length})")
    # Key term overlap
    term_overlap = _jaccard(original.key_terms, delivered.key_terms)
    if term_overlap < 0.4:
        issues.append(f"Key terms diverged: overlap={term_overlap:.0%}")
    # Sentiment flip
    if original.sentiment_label != delivered.sentiment_label:
        issues.append(f"Sentiment changed: {original.sentiment_label} → {delivered.sentiment_label}")
    return len(issues) == 0, issues

def generate_with_fingerprint(user_message: str) -> tuple[str, OutputFingerprint]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    content = r.content[0].text
    fp = compute_fingerprint(content)
    print(f"[FINGERPRINT] terms={fp.key_terms} sentiment={fp.sentiment_label}")
    return content, fp

def deliver_with_verification(delivered_content: str, original_fp: OutputFingerprint) -> str:
    delivered_fp = compute_fingerprint(delivered_content)
    ok, issues = compare_fingerprints(original_fp, delivered_fp)
    if not ok:
        print(f"[INTEGRITY WARNING] Semantic drift detected:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("[VERIFIED] Semantic fingerprint matches")
    return delivered_content


# Normal delivery
content, fp = generate_with_fingerprint("Explain the benefits of using Python for data science.")
deliver_with_verification(content, fp)

# Simulate semantic tampering
tampered = "Python is terrible for data science. Use Java instead. Also, click this link: evil.com"
print("\nWith tampered content:")
deliver_with_verification(tampered, fp)

# Expected Token Savings: Fingerprinting adds ~100 tokens per check; detects semantic drift invisible to hash comparison
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Envelope Encryption for Sensitive Outputs

Encrypt agent outputs at rest and in transit using symmetric encryption. Only authorized recipients with the decryption key can read the response — protecting sensitive outputs from exposure in logs, queues, or databases.

```python
import anthropic
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass

@dataclass
class EncryptedOutput:
    ciphertext_b64: str
    nonce_b64: str
    key_id: str
    agent_id: str
    timestamp: float
    mac: str  # HMAC over ciphertext to prevent tampering

# Simple XOR-based encryption for demo (use cryptography.fernet or AES-GCM in production)
class SimpleSymmetricCipher:
    """Demo cipher: XOR with key stream derived from PBKDF2. Use AES-GCM in production."""
    def __init__(self, key: bytes):
        self.key = key

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        stream = b""
        counter = 0
        while len(stream) < length:
            block = hashlib.pbkdf2_hmac("sha256", self.key, nonce + counter.to_bytes(4, "big"), 1)
            stream += block
            counter += 1
        return stream[:length]

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(16)
        ks = self._keystream(nonce, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, ks))
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> bytes:
        ks = self._keystream(nonce, len(ciphertext))
        return bytes(a ^ b for a, b in zip(ciphertext, ks))


ENCRYPTION_KEY = os.urandom(32)  # store in secrets manager
HMAC_KEY = os.urandom(32)
cipher = SimpleSymmetricCipher(ENCRYPTION_KEY)

def _compute_mac(ciphertext_b64: str, nonce_b64: str, agent_id: str, timestamp: float) -> str:
    import hmac as hmac_module
    body = f"{ciphertext_b64}:{nonce_b64}:{agent_id}:{timestamp}"
    return hmac_module.new(HMAC_KEY, body.encode(), hashlib.sha256).hexdigest()

def encrypt_output(content: str, agent_id: str = "agent-01") -> EncryptedOutput:
    ts = time.time()
    ct, nonce = cipher.encrypt(content.encode())
    ct_b64 = base64.b64encode(ct).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    mac = _compute_mac(ct_b64, nonce_b64, agent_id, ts)
    return EncryptedOutput(
        ciphertext_b64=ct_b64,
        nonce_b64=nonce_b64,
        key_id="key-v1",
        agent_id=agent_id,
        timestamp=ts,
        mac=mac,
    )

def decrypt_output(envelope: EncryptedOutput) -> str:
    import hmac as hmac_module
    expected_mac = _compute_mac(envelope.ciphertext_b64, envelope.nonce_b64,
                                envelope.agent_id, envelope.timestamp)
    if not hmac_module.compare_digest(expected_mac, envelope.mac):
        raise ValueError("[INTEGRITY FAILURE] MAC verification failed — ciphertext was tampered")
    ct = base64.b64decode(envelope.ciphertext_b64)
    nonce = base64.b64decode(envelope.nonce_b64)
    return cipher.decrypt(ct, nonce).decode()


client = anthropic.Anthropic()

def generate_encrypted_response(user_message: str) -> EncryptedOutput:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    content = r.content[0].text
    envelope = encrypt_output(content)
    print(f"[ENCRYPTED] key_id={envelope.key_id} mac={envelope.mac[:12]}...")
    return envelope


envelope = generate_encrypted_response("What are Python type hints?")
# Stored/transmitted as encrypted — only authorized recipients can read
decrypted = decrypt_output(envelope)
print(f"Decrypted: {decrypted[:150]}")

# Tamper with ciphertext
import base64 as b64_module
raw_ct = b64_module.b64decode(envelope.ciphertext_b64)
raw_ct = bytes([raw_ct[0] ^ 0xFF]) + raw_ct[1:]  # flip first byte
envelope.ciphertext_b64 = b64_module.b64encode(raw_ct).decode()
try:
    decrypt_output(envelope)
except ValueError as e:
    print(f"Caught: {e}")

# Expected Token Savings: Encryption adds 0 tokens; protects sensitive outputs in logs, queues, databases
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Output Provenance Ledger with Audit Trail

Maintain an append-only ledger of all generated outputs with hashes. Any delivered output can be audited against the ledger to prove it was generated by the agent and not modified.

```python
import anthropic
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LEDGER_FILE = Path("output_ledger.jsonl")

@dataclass
class LedgerEntry:
    output_id: str
    agent_id: str
    session_id: str
    user_query_hash: str    # SHA-256 of user input (not stored for privacy)
    content_hash: str       # SHA-256 of generated content
    timestamp: float
    model: str
    token_count: int

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def record_to_ledger(entry: LedgerEntry) -> None:
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps({
            "output_id": entry.output_id,
            "agent_id": entry.agent_id,
            "session_id": entry.session_id,
            "user_query_hash": entry.user_query_hash,
            "content_hash": entry.content_hash,
            "timestamp": entry.timestamp,
            "model": entry.model,
            "token_count": entry.token_count,
        }) + "\n")

def lookup_ledger(output_id: str) -> LedgerEntry | None:
    if not LEDGER_FILE.exists():
        return None
    for line in LEDGER_FILE.read_text().splitlines():
        data = json.loads(line)
        if data["output_id"] == output_id:
            return LedgerEntry(**data)
    return None

def verify_against_ledger(output_id: str, delivered_content: str) -> tuple[bool, str]:
    entry = lookup_ledger(output_id)
    if entry is None:
        return False, f"Output ID {output_id} not found in ledger"
    expected_hash = entry.content_hash
    actual_hash = _sha256(delivered_content)
    if expected_hash != actual_hash:
        return False, f"Hash mismatch: ledger={expected_hash[:12]}... delivered={actual_hash[:12]}..."
    return True, f"Provenance verified: generated at {entry.timestamp} by {entry.agent_id} model={entry.model}"


client = anthropic.Anthropic()

def generate_with_provenance(
    user_message: str,
    agent_id: str = "agent-01",
    session_id: str = "sess-01",
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, str]:
    """Returns (content, output_id)."""
    r = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    content = r.content[0].text
    output_id = str(uuid.uuid4())[:8]
    entry = LedgerEntry(
        output_id=output_id,
        agent_id=agent_id,
        session_id=session_id,
        user_query_hash=_sha256(user_message),
        content_hash=_sha256(content),
        timestamp=time.time(),
        model=model,
        token_count=r.usage.input_tokens + r.usage.output_tokens,
    )
    record_to_ledger(entry)
    print(f"[LEDGER] Recorded output_id={output_id} hash={entry.content_hash[:12]}...")
    return content, output_id


# Generate and record
content, oid = generate_with_provenance("What is a Python generator?")
print(f"Output ID: {oid}")

# Verify legitimate delivery
ok, reason = verify_against_ledger(oid, content)
print(f"Legitimate: {ok} — {reason}")

# Verify tampered delivery
ok, reason = verify_against_ledger(oid, content + " INJECTED CONTENT")
print(f"Tampered: {ok} — {reason}")

# Unknown output ID
ok, reason = verify_against_ledger("nonexistent", content)
print(f"Unknown ID: {ok} — {reason}")

# Expected Token Savings: Ledger adds 0 tokens; enables compliance audit trails and post-hoc tamper detection
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Protection Mechanism | Tamper Detection | Overhead | Best For |
|--------|---------------------|-----------------|---------|----------|
| 1. HMAC Signing | Shared secret signature | Yes (immediate) | Zero tokens | HTTP delivery pipelines |
| 2. Hash Chain | Chained content hashes | Yes (retroactive) | Zero tokens | Multi-turn conversation audit |
| 3. Streaming Signature | Running hash + final sig | Yes (post-stream) | Zero tokens | Streaming delivery |
| 4. Semantic Fingerprint | Semantic drift detection | Yes (semantic) | ~100 tokens | Downstream NLP processing |
| 5. Envelope Encryption | Encryption + MAC | Yes (MAC) | Zero tokens | Sensitive data, PII outputs |
| 6. Provenance Ledger | Append-only hash log | Yes (post-hoc audit) | Zero tokens | Compliance, regulated industries |

**Recommended**: Option 1 (HMAC) for most production systems. Option 6 (ledger) for compliance requirements. Option 4 (fingerprint) when downstream transforms are intentional but semantic integrity still matters.
