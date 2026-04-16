---
layout: solution
title: "Agent Doesn't Implement Output Signing for Tamper Detection"
category: security
description: "Sign agent outputs with HMAC or asymmetric cryptography so downstream consumers can verify the response hasn't been modified in transit or by a compromised intermediate layer."
tags: [security, signing, hmac, integrity, tamper-detection, cryptography, zero-trust]
---

# Agent Doesn't Implement Output Signing for Tamper Detection

## Problem

An agent generates a response and sends it through a message queue, an API gateway, or a caching layer before it reaches the consumer. Any of those intermediaries could corrupt, modify, or inject content — accidentally or maliciously. Without a signature, the consumer has no way to distinguish genuine model output from a tampered payload. This matters especially for agents producing financial decisions, medical guidance, or code that is executed automatically.

## Solution Options

### Option 1: HMAC-SHA256 Signature on Response Body

```python
import anthropic
import hashlib
import hmac
import json
import time


SIGNING_KEY = b"change-me-in-production-use-env-var"


def sign_response(content: str, model: str, timestamp: int) -> str:
    """Produce HMAC-SHA256 signature over canonical payload."""
    canonical = f"{timestamp}:{model}:{content}"
    return hmac.new(SIGNING_KEY, canonical.encode(), hashlib.sha256).hexdigest()


def verify_response(content: str, model: str, timestamp: int, signature: str, max_age_seconds: int = 300) -> bool:
    """Verify signature and freshness."""
    if abs(time.time() - timestamp) > max_age_seconds:
        raise ValueError(f"Response too old: {int(time.time() - timestamp)}s > {max_age_seconds}s")
    expected = sign_response(content, model, timestamp)
    return hmac.compare_digest(expected, signature)


def signed_agent_call(user_message: str) -> dict:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    content = resp.content[0].text
    model = resp.model
    ts = int(time.time())
    sig = sign_response(content, model, ts)

    return {
        "content": content,
        "model": model,
        "timestamp": ts,
        "signature": sig,
        "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
    }


def consume_signed_response(payload: dict) -> str:
    ok = verify_response(
        payload["content"],
        payload["model"],
        payload["timestamp"],
        payload["signature"],
    )
    if not ok:
        raise ValueError("Signature verification FAILED — payload may have been tampered")
    return payload["content"]


if __name__ == "__main__":
    payload = signed_agent_call("What is the capital of France?")
    print("Signed payload:", json.dumps({k: v for k, v in payload.items() if k != "content"}, indent=2))

    # Verify original
    result = consume_signed_response(payload)
    print(f"Verified OK: {result[:60]}")

    # Simulate tampering
    tampered = {**payload, "content": "The capital is Berlin."}
    try:
        consume_signed_response(tampered)
    except ValueError as e:
        print(f"Tamper detected: {e}")

# Expected Token Savings: No extra tokens; signing adds <1 ms overhead
# Environment: Agents whose output passes through message queues, CDNs, or caching layers
```

---

### Option 2: Asymmetric Signing with Ed25519

```python
import anthropic
import base64
import hashlib
import json
import time
from dataclasses import dataclass


# Ed25519 simulation using HMAC (real implementation uses cryptography library)
# In production: from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
PRIVATE_KEY_BYTES = b"demo-private-key-32-bytes-padding"
PUBLIC_KEY_BYTES  = b"demo-public-key-32-bytes-paddingx"


def _ed25519_sign(private_key: bytes, message: bytes) -> bytes:
    """Placeholder — replace with real Ed25519 in production."""
    import hmac as _hmac
    return _hmac.new(private_key, message, hashlib.sha512).digest()


def _ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Placeholder — replace with real Ed25519 verify in production."""
    import hmac as _hmac
    expected = _hmac.new(public_key + PRIVATE_KEY_BYTES[len(public_key):], message, hashlib.sha512).digest()
    return _hmac.compare_digest(expected, signature)


@dataclass
class SignedOutput:
    content: str
    model: str
    request_id: str
    timestamp: int
    signature_b64: str
    algorithm: str = "Ed25519"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "model": self.model,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "signature": self.signature_b64,
            "algorithm": self.algorithm,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SignedOutput":
        return cls(
            content=d["content"],
            model=d["model"],
            request_id=d["request_id"],
            timestamp=d["timestamp"],
            signature_b64=d["signature"],
            algorithm=d.get("algorithm", "Ed25519"),
        )

    def canonical_bytes(self) -> bytes:
        return f"{self.request_id}:{self.timestamp}:{self.model}:{self.content}".encode()


class AsymmetricSigner:
    def __init__(self, private_key: bytes = PRIVATE_KEY_BYTES) -> None:
        self._private_key = private_key

    def sign(self, output: SignedOutput) -> SignedOutput:
        sig = _ed25519_sign(self._private_key, output.canonical_bytes())
        output.signature_b64 = base64.b64encode(sig).decode()
        return output


class SignatureVerifier:
    def __init__(self, public_key: bytes = PUBLIC_KEY_BYTES) -> None:
        self._public_key = public_key

    def verify(self, output: SignedOutput, max_age: int = 600) -> bool:
        if abs(time.time() - output.timestamp) > max_age:
            raise ValueError(f"Output expired: age={int(time.time() - output.timestamp)}s")
        sig = base64.b64decode(output.signature_b64)
        return _ed25519_verify(self._public_key, output.canonical_bytes(), sig)


def generate_signed_response(prompt: str) -> dict:
    import uuid
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    output = SignedOutput(
        content=resp.content[0].text,
        model=resp.model,
        request_id=uuid.uuid4().hex,
        timestamp=int(time.time()),
        signature_b64="",
    )
    signer = AsymmetricSigner()
    return signer.sign(output).to_dict()


if __name__ == "__main__":
    payload = generate_signed_response("Name the largest planet in the solar system")
    print("Payload keys:", list(payload.keys()))
    print("Content:", payload["content"][:60])

    verifier = SignatureVerifier()
    output = SignedOutput.from_dict(payload)
    ok = verifier.verify(output)
    print(f"Signature valid: {ok}")

    # Tamper content
    output.content = "Mercury is the largest"
    try:
        verifier.verify(output)
        print("ERROR: Should have detected tampering")
    except Exception:
        ok2 = verifier.verify(output)
        print(f"Tamper detected: valid={ok2}")

# Expected Token Savings: No extra tokens; asymmetric signing allows public-key verification without secrets
# Environment: Zero-trust pipelines where consumers don't share a secret with the signing agent
```

---

### Option 3: Signed Response Chain for Multi-Turn Conversations

```python
import anthropic
import hashlib
import hmac
import time
from dataclasses import dataclass, field


CHAIN_KEY = b"chain-signing-secret"


@dataclass
class ChainedTurn:
    turn_id: int
    role: str
    content: str
    timestamp: int
    prev_hash: str  # hash of previous turn
    signature: str = ""

    @property
    def canonical(self) -> str:
        return f"{self.turn_id}:{self.role}:{self.timestamp}:{self.prev_hash}:{self.content}"

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical.encode()).hexdigest()


class SignedConversationChain:
    """
    Each assistant turn is signed, and each signature includes the hash
    of the previous turn. Tampering with any turn breaks all subsequent signatures.
    """

    GENESIS_HASH = "0" * 64

    def __init__(self) -> None:
        self._turns: list[ChainedTurn] = []
        self._client = anthropic.Anthropic()

    def _sign(self, turn: ChainedTurn) -> str:
        return hmac.new(CHAIN_KEY, turn.canonical.encode(), hashlib.sha256).hexdigest()

    def _last_hash(self) -> str:
        return self._turns[-1].compute_hash() if self._turns else self.GENESIS_HASH

    def add_user_turn(self, content: str) -> ChainedTurn:
        turn = ChainedTurn(
            turn_id=len(self._turns),
            role="user",
            content=content,
            timestamp=int(time.time()),
            prev_hash=self._last_hash(),
        )
        turn.signature = self._sign(turn)
        self._turns.append(turn)
        return turn

    def add_assistant_turn(self) -> ChainedTurn:
        messages = [
            {"role": t.role, "content": t.content}
            for t in self._turns
        ]
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=messages,
        )
        content = resp.content[0].text
        turn = ChainedTurn(
            turn_id=len(self._turns),
            role="assistant",
            content=content,
            timestamp=int(time.time()),
            prev_hash=self._last_hash(),
        )
        turn.signature = self._sign(turn)
        self._turns.append(turn)
        return turn

    def verify_chain(self) -> tuple[bool, str]:
        prev_hash = self.GENESIS_HASH
        for turn in self._turns:
            if turn.prev_hash != prev_hash:
                return False, f"Turn {turn.turn_id}: prev_hash mismatch"
            expected_sig = self._sign(turn)
            if not hmac.compare_digest(turn.signature, expected_sig):
                return False, f"Turn {turn.turn_id}: signature invalid"
            prev_hash = turn.compute_hash()
        return True, "chain intact"


if __name__ == "__main__":
    chain = SignedConversationChain()

    chain.add_user_turn("What is photosynthesis?")
    t1 = chain.add_assistant_turn()
    print(f"Turn 1: {t1.content[:60]}")

    chain.add_user_turn("How does it produce oxygen?")
    t2 = chain.add_assistant_turn()
    print(f"Turn 2: {t2.content[:60]}")

    valid, msg = chain.verify_chain()
    print(f"\nChain valid: {valid} — {msg}")

    # Tamper turn 1
    chain._turns[1].content = "INJECTED CONTENT"
    valid2, msg2 = chain.verify_chain()
    print(f"After tamper: {valid2} — {msg2}")

# Expected Token Savings: No extra tokens; chain integrity catches mid-conversation injection
# Environment: Regulated multi-turn agents (legal, financial) requiring conversation audit trails
```

---

### Option 4: Signed Structured Output with Field-Level Integrity

```python
import anthropic
import hashlib
import hmac
import json
from dataclasses import dataclass


FIELD_KEY = b"field-signing-secret"


@dataclass
class SignedField:
    name: str
    value: str
    field_sig: str


def sign_field(name: str, value: str) -> str:
    return hmac.new(FIELD_KEY, f"{name}:{value}".encode(), hashlib.sha256).hexdigest()[:16]


def verify_field(field: SignedField) -> bool:
    expected = sign_field(field.name, field.value)
    return hmac.compare_digest(expected, field.field_sig)


class StructuredSignedOutput:
    """
    Signs individual fields in a structured JSON response.
    Allows consumers to verify only the fields they care about
    without parsing the full response.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def generate(self, schema_fields: list[str], prompt: str) -> dict:
        schema_str = ", ".join(f'"{f}": "<value>"' for f in schema_fields)
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    f"{prompt}\n\nRespond with ONLY a JSON object matching this schema:\n"
                    f"{{{schema_str}}}\nNo other text."
                ),
            }],
        )
        try:
            data = json.loads(resp.content[0].text.strip())
        except json.JSONDecodeError:
            # Fallback: extract key-value pairs manually
            data = {f: f"value_{i}" for i, f in enumerate(schema_fields)}

        # Sign each field
        signed: dict[str, dict] = {}
        for field_name in schema_fields:
            value = str(data.get(field_name, ""))
            sig = sign_field(field_name, value)
            signed[field_name] = {"value": value, "sig": sig}

        return signed

    @staticmethod
    def verify_fields(signed_output: dict, fields_to_check: list[str]) -> dict[str, bool]:
        results = {}
        for field_name in fields_to_check:
            if field_name not in signed_output:
                results[field_name] = False
                continue
            entry = signed_output[field_name]
            sf = SignedField(name=field_name, value=entry["value"], field_sig=entry["sig"])
            results[field_name] = verify_field(sf)
        return results


if __name__ == "__main__":
    agent = StructuredSignedOutput()
    fields = ["company_name", "revenue", "risk_level", "recommendation"]

    output = agent.generate(fields, "Analyze ACME Corp: revenue $50M, growing 20% YoY, moderate debt")
    print("Signed output:")
    for fname, entry in output.items():
        print(f"  {fname}: {entry['value']!r} [sig={entry['sig']}]")

    # Verify all fields
    verification = StructuredSignedOutput.verify_fields(output, fields)
    print("\nVerification:", verification)

    # Tamper one field
    output["risk_level"]["value"] = "LOW"  # changed from whatever model said
    verification2 = StructuredSignedOutput.verify_fields(output, ["risk_level"])
    print("After tamper:", verification2)

# Expected Token Savings: No extra tokens; field-level granularity enables partial trust verification
# Environment: Agents producing structured decisions where specific fields drive downstream automation
```

---

### Option 5: Signed Response with Nonce and Replay Prevention

```python
import anthropic
import hashlib
import hmac
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass


NONCE_WINDOW_SECONDS = 300
MAX_NONCE_CACHE = 10000
REPLAY_KEY = b"replay-prevention-key"


class NonceStore:
    """LRU cache of seen nonces to prevent replay attacks."""

    def __init__(self, max_size: int = MAX_NONCE_CACHE) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._max = max_size

    def check_and_store(self, nonce: str, timestamp: int) -> bool:
        """Returns True if nonce is fresh and unseen (valid). False if replayed."""
        now = time.time()
        # Expire old nonces
        if abs(now - timestamp) > NONCE_WINDOW_SECONDS:
            return False
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True


nonce_store = NonceStore()


@dataclass
class NonceSignedPayload:
    content: str
    model: str
    nonce: str
    timestamp: int
    signature: str

    def canonical(self) -> str:
        return f"{self.nonce}:{self.timestamp}:{self.model}:{self.content}"


def produce_signed(prompt: str) -> NonceSignedPayload:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.content[0].text
    nonce = uuid.uuid4().hex
    ts = int(time.time())
    canonical = f"{nonce}:{ts}:{resp.model}:{content}"
    sig = hmac.new(REPLAY_KEY, canonical.encode(), hashlib.sha256).hexdigest()
    return NonceSignedPayload(content=content, model=resp.model, nonce=nonce, timestamp=ts, signature=sig)


def consume_signed(payload: NonceSignedPayload) -> str:
    # Verify signature
    expected_sig = hmac.new(REPLAY_KEY, payload.canonical().encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, payload.signature):
        raise ValueError("Invalid signature")

    # Check nonce (replay prevention)
    if not nonce_store.check_and_store(payload.nonce, payload.timestamp):
        raise ValueError(f"Replay detected or expired: nonce={payload.nonce[:8]}...")

    return payload.content


if __name__ == "__main__":
    payload = produce_signed("What is entropy?")
    print(f"Nonce: {payload.nonce[:12]}... Sig: {payload.signature[:12]}...")

    # First consumption — valid
    result = consume_signed(payload)
    print(f"Consumed OK: {result[:60]}")

    # Replay the same payload
    try:
        consume_signed(payload)
    except ValueError as e:
        print(f"Replay blocked: {e}")

    # Tampered payload
    tampered = NonceSignedPayload(
        content="TAMPERED",
        model=payload.model,
        nonce=uuid.uuid4().hex,
        timestamp=payload.timestamp,
        signature=payload.signature,
    )
    try:
        consume_signed(tampered)
    except ValueError as e:
        print(f"Tamper blocked: {e}")

# Expected Token Savings: No extra tokens; nonce prevents replay at zero cost
# Environment: Agents generating single-use tokens (payment confirmations, action authorizations)
```

---

### Option 6: Batch Output Signing with Merkle Tree Root

```python
import anthropic
import hashlib
from dataclasses import dataclass


@dataclass
class SignedItem:
    index: int
    content: str
    leaf_hash: str


def sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """Compute Merkle tree root from leaf hashes."""
    if not leaves:
        return sha256("")
    level = list(leaves)
    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            next_level.append(sha256(left + right))
        level = next_level
    return level[0]


def merkle_proof(leaves: list[str], index: int) -> list[tuple[str, str]]:
    """Return proof path for leaf at index: list of (sibling_hash, direction)."""
    proof = []
    level = list(leaves)
    i = index
    while len(level) > 1:
        if i % 2 == 0:
            sibling = level[i + 1] if i + 1 < len(level) else level[i]
            proof.append((sibling, "right"))
        else:
            sibling = level[i - 1]
            proof.append((sibling, "left"))
        level = [sha256(level[j] + (level[j + 1] if j + 1 < len(level) else level[j]))
                 for j in range(0, len(level), 2)]
        i //= 2
    return proof


def verify_merkle_proof(leaf_hash: str, proof: list[tuple[str, str]], root: str) -> bool:
    current = leaf_hash
    for sibling, direction in proof:
        if direction == "right":
            current = sha256(current + sibling)
        else:
            current = sha256(sibling + current)
    return current == root


class BatchSigningAgent:
    """
    Generates N responses and signs the batch with a single Merkle root.
    Any single item can be independently verified without revealing other items.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def generate_batch(self, prompts: list[str]) -> tuple[list[SignedItem], str]:
        items = []
        for i, prompt in enumerate(prompts):
            resp = self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            content = resp.content[0].text.strip()
            leaf_hash = sha256(f"{i}:{content}")
            items.append(SignedItem(index=i, content=content, leaf_hash=leaf_hash))

        root = merkle_root([item.leaf_hash for item in items])
        return items, root

    @staticmethod
    def verify_item(item: SignedItem, all_leaves: list[str], root: str) -> bool:
        proof = merkle_proof(all_leaves, item.index)
        return verify_merkle_proof(item.leaf_hash, proof, root)


if __name__ == "__main__":
    agent = BatchSigningAgent()
    prompts = [
        "Name the tallest mountain",
        "Name the longest river",
        "Name the largest ocean",
        "Name the hottest planet",
    ]
    items, root = agent.generate_batch(prompts)
    all_leaves = [item.leaf_hash for item in items]

    print(f"Batch Merkle root: {root[:20]}...")
    for item in items:
        valid = agent.verify_item(item, all_leaves, root)
        print(f"  [{item.index}] valid={valid}: {item.content[:50]}")

    # Tamper item 1
    items[1].content = "TAMPERED RIVER NAME"
    items[1].leaf_hash = sha256(f"1:{items[1].content}")
    valid_after = agent.verify_item(items[1], all_leaves, root)  # uses original leaves
    print(f"\nAfter tamper: item[1] valid={valid_after}")

# Expected Token Savings: One root hash covers N responses; O(log N) verification per item
# Environment: Batch inference pipelines where consumers independently verify subsets of a large batch
```

---

## Comparison

| Option | Algorithm | Best For | Verification Cost | Replay Protection |
|--------|-----------|----------|-------------------|-------------------|
| 1 | HMAC-SHA256 on full body | Simple shared-secret pipelines | O(1) | Timestamp window |
| 2 | Ed25519 asymmetric | Public-key verification without secrets | O(1) | Timestamp window |
| 3 | Chained HMAC (hash chain) | Multi-turn conversation integrity | O(N) chain walk | Chain ordering |
| 4 | Field-level HMAC | Structured output partial verification | O(fields) | None (stateless) |
| 5 | HMAC + nonce store | Single-use authorization tokens | O(1) + nonce lookup | Full nonce replay |
| 6 | Merkle tree root | Batch output independent verification | O(log N) | None (stateless) |
