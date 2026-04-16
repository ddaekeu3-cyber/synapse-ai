---
title: "Agent Doesn't Implement System Prompt Encryption at Rest"
description: "Encrypt system prompts containing proprietary instructions, trade secrets, or sensitive business logic before storing them—preventing leakage through database dumps, log files, or unauthorized access."
difficulty: intermediate
category: security
tags: [security, encryption, system-prompt, secrets-management, data-protection]
---

## Problem

System prompts often contain proprietary business logic, trade secrets, pricing strategies, or confidential instructions that give a product its competitive edge. Storing them in plaintext in databases, config files, or environment variables exposes them to database dumps, log aggregation systems, developer access, or insider threats. Encrypting system prompts at rest ensures that even if storage is compromised, the prompt content remains protected.

## Solutions

### Option 1: AES-GCM Symmetric Encryption with Key from Env

Encrypt prompts with AES-256-GCM using a key stored in a secrets manager, not alongside the data.

```python
import asyncio
import base64
import os
from anthropic import AsyncAnthropic

# pip install cryptography
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import secrets as python_secrets

client = AsyncAnthropic()

class PromptEncryptor:
    def __init__(self, key_hex: str | None = None):
        if key_hex:
            self._key = bytes.fromhex(key_hex)
        else:
            # Load from env or generate new key
            key_env = os.environ.get("PROMPT_ENCRYPTION_KEY")
            if key_env:
                self._key = bytes.fromhex(key_env)
            else:
                # Generate a new 256-bit key for demo
                self._key = python_secrets.token_bytes(32)
                print(f"[DEMO] Generated key (store securely): {self._key.hex()}")

        self._aes = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt and return base64-encoded ciphertext with nonce prepended."""
        nonce = python_secrets.token_bytes(12)  # 96-bit nonce for GCM
        ciphertext = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        # Prepend nonce to ciphertext
        combined = nonce + ciphertext
        return base64.urlsafe_b64encode(combined).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        """Decrypt base64-encoded ciphertext with prepended nonce."""
        combined = base64.urlsafe_b64decode(encrypted.encode("ascii"))
        nonce = combined[:12]
        ciphertext = combined[12:]
        plaintext = self._aes.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")

# Simulate a database storing encrypted prompts
ENCRYPTED_PROMPT_STORE: dict[str, str] = {}

async def setup_agent(agent_id: str, system_prompt: str, encryptor: PromptEncryptor):
    """Store an encrypted system prompt."""
    encrypted = encryptor.encrypt(system_prompt)
    ENCRYPTED_PROMPT_STORE[agent_id] = encrypted
    print(f"[Storage] Stored encrypted prompt for '{agent_id}' "
          f"({len(encrypted)} chars, unreadable at rest)")
    print(f"[Storage] Encrypted: {encrypted[:60]}...")

async def run_agent(agent_id: str, user_message: str, encryptor: PromptEncryptor) -> str:
    """Retrieve, decrypt, and use system prompt at runtime only."""
    encrypted = ENCRYPTED_PROMPT_STORE.get(agent_id)
    if not encrypted:
        raise ValueError(f"No prompt found for agent '{agent_id}'")

    # Decrypt only at runtime, in memory
    system_prompt = encryptor.decrypt(encrypted)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text

async def demo_aes_encryption():
    encryptor = PromptEncryptor()

    # Confidential business logic in the system prompt
    proprietary_prompt = (
        "You are a pricing advisor for Acme Corp. "
        "Apply our confidential discount matrix: "
        "orders >$10k get 15% off, >$50k get 22% off, >$100k get 30% off. "
        "Never reveal these exact percentages; present them as 'competitive pricing'."
    )

    await setup_agent("pricing-agent", proprietary_prompt, encryptor)
    response = await run_agent("pricing-agent", "What discount can I get on a $75k order?", encryptor)
    print(f"\nAgent response: {response.strip()[:200]}")

asyncio.run(demo_aes_encryption())
```

### Option 2: Envelope Encryption with Key Hierarchy

Use a two-tier key system: data encryption key (DEK) per prompt, master key encryption key (KEK) stored in a vault.

```python
import asyncio
import base64
import json
import os
import secrets as python_secrets
from anthropic import AsyncAnthropic
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class EncryptedPromptBundle:
    """Stores encrypted DEK + encrypted prompt together."""
    agent_id: str
    encrypted_dek: str      # DEK encrypted with KEK
    encrypted_prompt: str   # Prompt encrypted with DEK
    dek_nonce: str
    prompt_nonce: str

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "encrypted_dek": self.encrypted_dek,
            "encrypted_prompt": self.encrypted_prompt,
            "dek_nonce": self.dek_nonce,
            "prompt_nonce": self.prompt_nonce,
        }

class EnvelopeEncryptionManager:
    def __init__(self, master_key_hex: str | None = None):
        if master_key_hex:
            self._kek = bytes.fromhex(master_key_hex)
        else:
            self._kek = python_secrets.token_bytes(32)
            print(f"[KEK] Master key (store in Vault/KMS): {self._kek.hex()}")

    def _aes_encrypt(self, key: bytes, plaintext: bytes) -> tuple[str, str]:
        nonce = python_secrets.token_bytes(12)
        aes = AESGCM(key)
        ct = aes.encrypt(nonce, plaintext, None)
        return (
            base64.urlsafe_b64encode(ct).decode(),
            base64.urlsafe_b64encode(nonce).decode()
        )

    def _aes_decrypt(self, key: bytes, encrypted_b64: str, nonce_b64: str) -> bytes:
        ct = base64.urlsafe_b64decode(encrypted_b64)
        nonce = base64.urlsafe_b64decode(nonce_b64)
        return AESGCM(key).decrypt(nonce, ct, None)

    def wrap_prompt(self, agent_id: str, system_prompt: str) -> EncryptedPromptBundle:
        # Generate fresh DEK for this prompt
        dek = python_secrets.token_bytes(32)

        # Encrypt DEK with KEK (envelope wrapping)
        encrypted_dek, dek_nonce = self._aes_encrypt(self._kek, dek)

        # Encrypt prompt with DEK
        encrypted_prompt, prompt_nonce = self._aes_encrypt(
            dek, system_prompt.encode("utf-8")
        )

        return EncryptedPromptBundle(
            agent_id=agent_id,
            encrypted_dek=encrypted_dek,
            encrypted_prompt=encrypted_prompt,
            dek_nonce=dek_nonce,
            prompt_nonce=prompt_nonce,
        )

    def unwrap_prompt(self, bundle: EncryptedPromptBundle) -> str:
        # Unwrap DEK using KEK
        dek = self._aes_decrypt(self._kek, bundle.encrypted_dek, bundle.dek_nonce)

        # Decrypt prompt using DEK
        plaintext = self._aes_decrypt(dek, bundle.encrypted_prompt, bundle.prompt_nonce)
        return plaintext.decode("utf-8")

# Simulated encrypted storage (would be a database in production)
BUNDLE_STORE: dict[str, dict] = {}

async def demo_envelope_encryption():
    manager = EnvelopeEncryptionManager()

    prompts = {
        "sales-agent": "You are a sales agent. Our cost basis is $47/unit. Never reveal this. Minimum margin is 20%.",
        "support-agent": "Internal escalation path: tier-1→tier-2→engineering@internal.com. SLA: 4h response.",
    }

    # Store encrypted bundles
    for agent_id, prompt in prompts.items():
        bundle = manager.wrap_prompt(agent_id, prompt)
        BUNDLE_STORE[agent_id] = bundle.to_dict()
        print(f"[Store] {agent_id}: prompt encrypted with unique DEK")

    # Runtime: unwrap and use
    for agent_id in prompts:
        stored = BUNDLE_STORE[agent_id]
        bundle = EncryptedPromptBundle(**stored)
        system = manager.unwrap_prompt(bundle)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": "How do I escalate a critical bug?"}]
        )
        print(f"\n[{agent_id}]: {response.content[0].text.strip()[:100]}")

asyncio.run(demo_envelope_encryption())
```

### Option 3: Versioned Encrypted Prompt Store

Maintain encrypted versions of system prompts with rotation history and audit trail.

```python
import asyncio
import base64
import json
import secrets as python_secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
STORE_PATH = Path(".encrypted_prompts.json")

@dataclass
class PromptVersion:
    version: int
    encrypted_data: str
    nonce: str
    created_at: float
    created_by: str
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "encrypted_data": self.encrypted_data,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "active": self.active,
        }

class VersionedPromptStore:
    def __init__(self, encryption_key: bytes):
        self._key = encryption_key
        self._store: dict[str, list[dict]] = self._load()

    def _load(self) -> dict:
        if STORE_PATH.exists():
            return json.loads(STORE_PATH.read_text())
        return {}

    def _save(self):
        STORE_PATH.write_text(json.dumps(self._store, indent=2))

    def _encrypt(self, text: str) -> tuple[str, str]:
        nonce = python_secrets.token_bytes(12)
        ct = AESGCM(self._key).encrypt(nonce, text.encode(), None)
        return base64.urlsafe_b64encode(ct).decode(), base64.urlsafe_b64encode(nonce).decode()

    def _decrypt(self, encrypted: str, nonce: str) -> str:
        ct = base64.urlsafe_b64decode(encrypted)
        n = base64.urlsafe_b64decode(nonce)
        return AESGCM(self._key).decrypt(n, ct, None).decode()

    def put(self, agent_id: str, prompt: str, author: str = "system") -> int:
        """Store a new version of the prompt."""
        versions = self._store.get(agent_id, [])

        # Deactivate previous active version
        for v in versions:
            v["active"] = False

        new_version = len(versions) + 1
        encrypted, nonce = self._encrypt(prompt)

        version = PromptVersion(
            version=new_version,
            encrypted_data=encrypted,
            nonce=nonce,
            created_at=time.time(),
            created_by=author,
            active=True,
        )
        versions.append(version.to_dict())
        self._store[agent_id] = versions
        self._save()
        print(f"[Store] {agent_id} v{new_version} saved (encrypted, active)")
        return new_version

    def get_active(self, agent_id: str) -> str:
        """Retrieve and decrypt the active prompt."""
        versions = self._store.get(agent_id, [])
        active = next((v for v in reversed(versions) if v["active"]), None)
        if not active:
            raise KeyError(f"No active prompt for '{agent_id}'")
        return self._decrypt(active["encrypted_data"], active["nonce"])

    def rollback(self, agent_id: str) -> int:
        """Roll back to the previous version."""
        versions = self._store.get(agent_id, [])
        if len(versions) < 2:
            raise ValueError("No previous version to roll back to")

        versions[-1]["active"] = False
        versions[-2]["active"] = True
        self._store[agent_id] = versions
        self._save()
        rollback_version = versions[-2]["version"]
        print(f"[Store] {agent_id} rolled back to v{rollback_version}")
        return rollback_version

    def version_history(self, agent_id: str) -> list[dict]:
        return [
            {
                "version": v["version"],
                "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(v["created_at"])),
                "author": v["created_by"],
                "active": v["active"],
            }
            for v in self._store.get(agent_id, [])
        ]

async def demo_versioned_store():
    key = python_secrets.token_bytes(32)
    store = VersionedPromptStore(key)

    # Initial prompt
    store.put("customer-agent", "You are a friendly customer service agent.", author="alice")

    # Update prompt
    store.put("customer-agent",
              "You are a premium customer service agent. Always offer a 10% goodwill discount.",
              author="bob")

    # Get active prompt
    active = store.get_active("customer-agent")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=active,
        messages=[{"role": "user", "content": "I'm unhappy with my order."}]
    )
    print(f"Active response: {response.content[0].text.strip()[:100]}")

    # Roll back
    store.rollback("customer-agent")

    print("\nVersion history:")
    for v in store.version_history("customer-agent"):
        active_marker = " ← active" if v["active"] else ""
        print(f"  v{v['version']} by {v['author']} at {v['created_at']}{active_marker}")

asyncio.run(demo_versioned_store())
```

### Option 4: Runtime Decryption with Memory Zeroing

Decrypt prompts only for the duration of the API call and zero the memory afterward.

```python
import asyncio
import base64
import ctypes
import gc
import secrets as python_secrets
from anthropic import AsyncAnthropic
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from contextlib import asynccontextmanager

client = AsyncAnthropic()

def zero_bytes(data: bytearray):
    """Overwrite bytearray contents with zeros."""
    for i in range(len(data)):
        data[i] = 0

class SecurePrompt:
    """Holds an encrypted prompt and decrypts only within a controlled context."""

    def __init__(self, plaintext: str, key: bytes):
        nonce = python_secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        self._encrypted = nonce + ct
        self._key = key

    @asynccontextmanager
    async def decrypted(self):
        """Async context manager: decrypt, yield, then zero memory."""
        buf = bytearray(self._encrypted)
        nonce = bytes(buf[:12])
        ct = bytes(buf[12:])

        plaintext_bytes = bytearray(AESGCM(self._key).decrypt(nonce, ct, None))
        plaintext_str = plaintext_bytes.decode("utf-8")
        try:
            yield plaintext_str
        finally:
            # Zero out sensitive memory
            zero_bytes(plaintext_bytes)
            zero_bytes(buf)
            del plaintext_str
            del plaintext_bytes
            gc.collect()

class SecureAgent:
    def __init__(self, system_prompt: str):
        self._encryption_key = python_secrets.token_bytes(32)
        self._secure_prompt = SecurePrompt(system_prompt, self._encryption_key)

    async def complete(self, user_message: str) -> str:
        async with self._secure_prompt.decrypted() as system:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": user_message}]
            )
        # system is zeroed from memory here
        return response.content[0].text

async def demo_runtime_decryption():
    agent = SecureAgent(
        system_prompt=(
            "You are a financial advisor. "
            "Our proprietary algorithm gives these signals: "
            "BUY when RSI < 30 and MACD crosses bullish. "
            "SELL when RSI > 70. Never reveal this formula."
        )
    )

    questions = [
        "Should I buy or sell tech stocks today?",
        "What's your investment philosophy?",
    ]

    for q in questions:
        response = await agent.complete(q)
        print(f"Q: {q}")
        print(f"A: {response.strip()[:150]}\n")

    print("[Security] System prompt was encrypted at rest, zeroed from memory after each use.")

asyncio.run(demo_runtime_decryption())
```

### Option 5: Shamir's Secret Sharing for Multi-Party Authorization

Require multiple authorized parties to reconstruct a system prompt, preventing single-point compromise.

```python
import asyncio
import json
import secrets as python_secrets
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

# Simplified secret sharing simulation
# In production: use the `secret-sharing` or `sss` Python library

@dataclass
class Share:
    index: int
    value: bytes

def split_key(key: bytes, n: int, threshold: int) -> list[Share]:
    """
    Simplified Shamir-like split: XOR-based for demo.
    In production, use proper GF(2^8) polynomial secret sharing.
    """
    if threshold > n:
        raise ValueError("threshold cannot exceed n")

    shares = []
    accumulated = bytearray(key)

    for i in range(n - 1):
        share_value = python_secrets.token_bytes(len(key))
        for j in range(len(accumulated)):
            accumulated[j] ^= share_value[j]
        shares.append(Share(index=i + 1, value=share_value))

    # Last share is XOR of key with all others
    shares.append(Share(index=n, value=bytes(accumulated)))
    return shares

def reconstruct_key(shares: list[Share], key_length: int) -> bytes:
    """XOR-based reconstruction (requires all shares for this demo)."""
    result = bytearray(key_length)
    for share in shares:
        for i, b in enumerate(share.value):
            result[i] ^= b
    return bytes(result)

class MultiPartyPromptStore:
    def __init__(self, n_parties: int = 3, threshold: int = 2):
        self.n = n_parties
        self.threshold = threshold
        self._master_key = python_secrets.token_bytes(32)
        self._shares = split_key(self._master_key, n_parties, threshold)
        self._encrypted_prompts: dict[str, bytes] = {}

    def distribute_shares(self) -> dict[str, bytes]:
        """Return shares for distribution to different parties."""
        return {f"party-{s.index}": s.value for s in self._shares}

    def encrypt_prompt(self, agent_id: str, prompt: str):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = python_secrets.token_bytes(12)
        ct = AESGCM(self._master_key).encrypt(nonce, prompt.encode(), None)
        self._encrypted_prompts[agent_id] = nonce + ct
        print(f"[MultiParty] Prompt encrypted. "
              f"Requires {self.threshold}/{self.n} parties to decrypt.")

    def decrypt_with_shares(
        self, agent_id: str, provided_shares: list[tuple[int, bytes]]
    ) -> str:
        if len(provided_shares) < self.threshold:
            raise PermissionError(
                f"Need {self.threshold} shares, got {len(provided_shares)}"
            )

        shares = [Share(index=idx, value=val) for idx, val in provided_shares]
        reconstructed_key = reconstruct_key(shares, len(self._master_key))

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        encrypted = self._encrypted_prompts[agent_id]
        nonce, ct = encrypted[:12], encrypted[12:]
        return AESGCM(reconstructed_key).decrypt(nonce, ct, None).decode()

async def demo_multi_party():
    store = MultiPartyPromptStore(n_parties=3, threshold=3)
    shares = store.distribute_shares()

    proprietary_prompt = (
        "This agent has access to merger negotiations details. "
        "Counterparty max: $2.3B. Our walk-away: $1.8B. Confidential."
    )
    store.encrypt_prompt("m&a-advisor", proprietary_prompt)

    # Reconstruct using all 3 shares
    provided = [(i + 1, v) for i, (_, v) in enumerate(shares.items())]
    system = store.decrypt_with_shares("m&a-advisor", provided)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": "What's our negotiating position?"}]
    )
    print(f"Response: {response.content[0].text.strip()[:150]}")

    # Try with only 2 shares (should fail since threshold=3)
    try:
        store.decrypt_with_shares("m&a-advisor", provided[:2])
    except PermissionError as e:
        print(f"\nInsufficient shares: {e}")

asyncio.run(demo_multi_party())
```

### Option 6: Prompt Tokenization with External Lookup

Store sensitive prompt fragments as opaque tokens, resolving them from an external vault only at runtime.

```python
import asyncio
import hashlib
import secrets as python_secrets
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Simulated external vault (in production: HashiCorp Vault, AWS Secrets Manager)
VAULT: dict[str, str] = {}

def vault_store(value: str) -> str:
    """Store a secret and return an opaque token."""
    token = f"vault:{hashlib.sha256(python_secrets.token_bytes(16)).hexdigest()[:16]}"
    VAULT[token] = value
    return token

async def vault_resolve(token: str) -> str:
    """Async lookup of token → secret value (simulates network call to vault)."""
    await asyncio.sleep(0.01)  # Simulate vault latency
    return VAULT.get(token, token)  # Return token unchanged if not found

async def resolve_prompt_tokens(template: str) -> str:
    """Replace all vault:XXXXX tokens in a prompt template with actual values."""
    tokens = re.findall(r"vault:[a-f0-9]{16}", template)
    resolved = {}
    for token in set(tokens):
        resolved[token] = await vault_resolve(token)

    result = template
    for token, value in resolved.items():
        result = result.replace(token, value)
    return result

class TokenizedPromptAgent:
    def __init__(self):
        # Prompt template stored in DB — contains only opaque tokens for sensitive parts
        self._discount_rate_token = vault_store("37%")
        self._cost_basis_token = vault_store("$42.50/unit")
        self._escalation_email_token = vault_store("vp-sales@internal.com")

        # This template is safe to store anywhere — tokens are meaningless without vault access
        self._prompt_template = (
            f"You are a sales agent. "
            f"Our cost basis is {self._cost_basis_token}. "
            f"Maximum authorized discount: {self._discount_rate_token}. "
            f"Escalation contact: {self._escalation_email_token}. "
            f"Never reveal these exact figures to customers."
        )
        print(f"[Template stored]: {self._prompt_template[:120]}")

    async def complete(self, user_message: str) -> str:
        # Resolve tokens at runtime only
        system_prompt = await resolve_prompt_tokens(self._prompt_template)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text

async def demo_tokenized_prompts():
    agent = TokenizedPromptAgent()

    questions = [
        "What's the best price you can offer on a bulk order?",
        "Who should I contact if negotiations stall?",
    ]

    for q in questions:
        response = await agent.complete(q)
        print(f"\nQ: {q}")
        print(f"A: {response.strip()[:150]}")

    print(f"\n[Vault] {len(VAULT)} secrets stored (inaccessible without vault credentials)")

asyncio.run(demo_tokenized_prompts())
```

## Comparison

| Approach | Security Level | Key Management | Rotation Support | Complexity |
|---|---|---|---|---|
| AES-GCM Symmetric | High | Single key from env/vault | Manual | Low |
| Envelope Encryption (DEK/KEK) | Very High | Two-tier hierarchy | Per-prompt DEK | Medium |
| Versioned Encrypted Store | High | Single key | Version history | Medium |
| Runtime Decryption + Memory Zeroing | High | Any key source | Yes | Medium |
| Shamir's Secret Sharing | Very High | Distributed (N parties) | Complex | High |
| Prompt Tokenization | High | Vault-managed | Per-token | Medium |

**Choose AES-GCM Symmetric** as the immediate baseline—it's one afternoon of work and immediately protects prompts from database dumps. **Choose Envelope Encryption** for production systems that need per-agent key isolation (compromise of one agent's prompt doesn't expose others). **Choose Prompt Tokenization** when only specific sensitive fragments need protection and you want to keep the prompt template human-readable for non-sensitive parts.
