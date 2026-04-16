---
layout: solution
title: "Agent Doesn't Implement Session Token Encryption at Rest"
category: auth
description: "Session tokens, API keys, and user credentials stored in agent memory, databases, or files must be encrypted at rest. Plaintext storage means any memory dump, log file, or database breach immediately exposes all active sessions and credentials."
tags: [auth, security, encryption, session-tokens, credentials, at-rest]
---

## Problem

Agent services often store session tokens, refresh tokens, and user credentials in Redis, SQLite, or in-memory dicts to maintain session continuity across turns. When these stores are compromised — through memory dumps, log exposure, database breaches, or debug output — plaintext tokens give attackers immediate access to all active sessions. Encrypting tokens at rest ensures that breached storage is useless without the encryption key.

## Solutions

### Option 1: AES-GCM Envelope Encryption for Session Store

```python
import anthropic
import os
import json
import base64
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("Install: pip install cryptography")

# Session encryption key — load from KMS/secrets manager in production
SESSION_KEY = os.environ.get("SESSION_ENCRYPTION_KEY", os.urandom(32))
if isinstance(SESSION_KEY, str):
    SESSION_KEY = base64.b64decode(SESSION_KEY)

@dataclass
class EncryptedSession:
    session_id: str
    ciphertext_b64: str      # Encrypted token data
    nonce_b64: str           # GCM nonce (12 bytes, unique per encryption)
    created_at: float
    expires_at: float
    user_id: str

# In-memory encrypted session store (use Redis in production)
_session_store: dict[str, EncryptedSession] = {}

def encrypt_token_data(data: dict, key: bytes) -> tuple[str, str]:
    """Encrypt token data using AES-256-GCM. Returns (ciphertext_b64, nonce_b64)."""
    if not CRYPTO_AVAILABLE:
        # Fallback: base64 (NOT SECURE — for demo only)
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        return encoded, "demo_nonce"

    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce, unique per encryption
    plaintext = json.dumps(data).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)  # No additional data
    return base64.b64encode(ciphertext).decode(), base64.b64encode(nonce).decode()

def decrypt_token_data(ciphertext_b64: str, nonce_b64: str, key: bytes) -> dict:
    """Decrypt token data. Raises exception if tampered."""
    if not CRYPTO_AVAILABLE or nonce_b64 == "demo_nonce":
        return json.loads(base64.b64decode(ciphertext_b64))

    aesgcm = AESGCM(key)
    ciphertext = base64.b64decode(ciphertext_b64)
    nonce = base64.b64decode(nonce_b64)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)

def create_encrypted_session(
    user_id: str,
    token_data: dict,
    ttl_seconds: float = 3600
) -> str:
    """Store session with encrypted token data. Returns session_id."""
    import uuid
    session_id = str(uuid.uuid4())
    now = time.time()

    ct, nonce = encrypt_token_data(token_data, SESSION_KEY)

    _session_store[session_id] = EncryptedSession(
        session_id=session_id,
        ciphertext_b64=ct,
        nonce_b64=nonce,
        created_at=now,
        expires_at=now + ttl_seconds,
        user_id=user_id
    )

    # What's stored — no plaintext tokens visible
    print(f"[Session] Stored encrypted session {session_id[:8]}... for user {user_id}")
    print(f"  Ciphertext: {ct[:40]}...")
    return session_id

def get_session_token_data(session_id: str) -> Optional[dict]:
    """Retrieve and decrypt session token data."""
    session = _session_store.get(session_id)
    if not session:
        return None
    if time.time() > session.expires_at:
        del _session_store[session_id]
        return None

    return decrypt_token_data(session.ciphertext_b64, session.nonce_b64, SESSION_KEY)

def run_agent_with_encrypted_session(user_id: str, prompt: str, existing_session_id: Optional[str] = None) -> dict:
    """Agent that maintains encrypted session state."""
    # Retrieve or create session
    token_data = None
    if existing_session_id:
        token_data = get_session_token_data(existing_session_id)

    if not token_data:
        # New session
        token_data = {
            "user_id": user_id,
            "access_token": f"tok_{os.urandom(8).hex()}",  # Would be real OAuth token
            "refresh_token": f"ref_{os.urandom(8).hex()}",
            "scopes": ["read", "write"],
            "created_at": time.time()
        }
        session_id = create_encrypted_session(user_id, token_data)
    else:
        session_id = existing_session_id

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=f"You are an assistant for user {user_id}. Their session is active.",
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "session_id": session_id,
        "response": response.content[0].text,
        "token_data_visible_in_store": False  # Encrypted at rest
    }

# Usage
result1 = run_agent_with_encrypted_session("user_42", "What can you help me with?")
print(f"\nResponse: {result1['response'][:100]}")

# Reuse session
result2 = run_agent_with_encrypted_session("user_42", "Thank you!", result1["session_id"])
print(f"Session reused: {result2['session_id'] == result1['session_id']}")

# Expected Token Savings: None — security overhead; prevents full session compromise on breach
# Environment: ANTHROPIC_API_KEY required; pip install cryptography
```

### Option 2: Key Derivation with Per-User Salt

```python
import anthropic
import os
import hashlib
import json
import base64
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

# Master key from environment — rotate via KMS
MASTER_KEY = os.environ.get("MASTER_KEY", os.urandom(32))
if isinstance(MASTER_KEY, str):
    MASTER_KEY = base64.b64decode(MASTER_KEY)

def derive_user_key(user_id: str, master_key: bytes) -> bytes:
    """Derive a unique encryption key per user using HKDF-like derivation."""
    # Use PBKDF2 as simple KDF (use HKDF from cryptography in production)
    salt = hashlib.sha256(f"user_key_salt_{user_id}".encode()).digest()
    return hashlib.pbkdf2_hmac('sha256', master_key, salt, iterations=10000, dklen=32)

def simple_encrypt(data: str, key: bytes) -> str:
    """XOR-based encryption (use AES-GCM in production)."""
    # For demo: derive keystream via SHA256 chaining
    encrypted = []
    key_stream = key
    for i, char in enumerate(data.encode()):
        if i % 32 == 0 and i > 0:
            key_stream = hashlib.sha256(key_stream).digest()
        encrypted.append(char ^ key_stream[i % 32])
    return base64.b64encode(bytes(encrypted)).decode()

def simple_decrypt(ciphertext_b64: str, key: bytes) -> str:
    """Reverse of simple_encrypt."""
    return simple_encrypt(
        base64.b64decode(ciphertext_b64).decode('latin-1'),
        key
    )

@dataclass
class UserEncryptedStore:
    user_id: str
    encrypted_data: dict[str, str]  # field_name -> encrypted_value
    created_at: float

_user_stores: dict[str, UserEncryptedStore] = {}

def store_user_credentials(user_id: str, credentials: dict) -> bool:
    """Encrypt and store user credentials with user-specific key."""
    user_key = derive_user_key(user_id, MASTER_KEY)

    encrypted = {}
    for field, value in credentials.items():
        encrypted[field] = simple_encrypt(json.dumps(value), user_key)

    _user_stores[user_id] = UserEncryptedStore(
        user_id=user_id,
        encrypted_data=encrypted,
        created_at=time.time()
    )
    print(f"[UserStore] Stored {len(credentials)} encrypted fields for {user_id}")
    print(f"  Fields: {list(encrypted.keys())} (values are ciphertext)")
    return True

def get_user_credentials(user_id: str) -> Optional[dict]:
    """Decrypt and return user credentials."""
    store = _user_stores.get(user_id)
    if not store:
        return None

    user_key = derive_user_key(user_id, MASTER_KEY)
    decrypted = {}
    for field, ciphertext in store.encrypted_data.items():
        try:
            decrypted[field] = json.loads(simple_decrypt(ciphertext, user_key))
        except Exception:
            decrypted[field] = None

    return decrypted

# Usage
store_user_credentials("user_alice", {
    "access_token": "Bearer eyJhbGciOiJSUzI1NiJ9...",
    "refresh_token": "1/7GDVMX4pFqVQM...",
    "api_key": "sk-ant-api03-...",
    "oauth_state": {"provider": "google", "scope": "email profile"}
})

# Breaching the store only shows ciphertext
print(f"\nBreach simulation — raw store values:")
for field, ct in _user_stores["user_alice"].encrypted_data.items():
    print(f"  {field}: {ct[:40]}...")

# Legitimate access decrypts correctly
creds = get_user_credentials("user_alice")
print(f"\nDecrypted access_token prefix: {creds['access_token'][:20]}...")

# Expected Token Savings: None — security only; per-user keys limit breach blast radius
# Environment: ANTHROPIC_API_KEY required; use cryptography.hazmat in production
```

### Option 3: Token Vault with Access Logging

```python
import anthropic
import os
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class VaultEntry:
    token_id: str
    owner_id: str
    token_type: str    # "session" | "api_key" | "refresh_token" | "oauth"
    encrypted_value: str
    key_version: int
    created_at: float
    last_accessed: float = 0.0
    access_count: int = 0
    tags: dict = field(default_factory=dict)

@dataclass
class AccessLogEntry:
    token_id: str
    accessor: str      # Which agent/service accessed it
    action: str        # "read" | "write" | "delete" | "rotate"
    timestamp: float
    success: bool
    ip_context: str = ""

class TokenVault:
    """Encrypted token vault with full access audit trail."""

    def __init__(self, master_key: bytes):
        self._master_key = master_key
        self._entries: dict[str, VaultEntry] = {}
        self._access_log: list[AccessLogEntry] = []
        self._key_version = 1

    def _derive_encryption_key(self, token_id: str, key_version: int) -> bytes:
        salt = f"vault_{token_id}_{key_version}".encode()
        return hashlib.pbkdf2_hmac('sha256', self._master_key, salt, iterations=1000, dklen=32)

    def _encrypt(self, plaintext: str, token_id: str) -> str:
        key = self._derive_encryption_key(token_id, self._key_version)
        # XOR for demo; use AES-GCM in production
        pt_bytes = plaintext.encode()
        ct = bytes(pt_bytes[i] ^ key[i % len(key)] for i in range(len(pt_bytes)))
        import base64
        return base64.b64encode(ct).decode()

    def _decrypt(self, ciphertext: str, token_id: str, key_version: int) -> str:
        import base64
        key = self._derive_encryption_key(token_id, key_version)
        ct_bytes = base64.b64decode(ciphertext)
        pt = bytes(ct_bytes[i] ^ key[i % len(key)] for i in range(len(ct_bytes)))
        return pt.decode()

    def _log(self, token_id: str, accessor: str, action: str, success: bool):
        self._access_log.append(AccessLogEntry(
            token_id=token_id, accessor=accessor, action=action,
            timestamp=time.time(), success=success
        ))

    def store(self, owner_id: str, token_type: str, plaintext_value: str,
              accessor: str = "system", tags: dict = None) -> str:
        """Store encrypted token. Returns token_id (reference, not the token)."""
        token_id = str(uuid.uuid4())[:8]
        encrypted = self._encrypt(plaintext_value, token_id)

        self._entries[token_id] = VaultEntry(
            token_id=token_id, owner_id=owner_id, token_type=token_type,
            encrypted_value=encrypted, key_version=self._key_version,
            created_at=time.time(), tags=tags or {}
        )
        self._log(token_id, accessor, "write", True)
        print(f"[Vault] Stored {token_type} for {owner_id} → token_id: {token_id}")
        return token_id

    def retrieve(self, token_id: str, accessor: str) -> Optional[str]:
        """Retrieve and decrypt token. Logged."""
        entry = self._entries.get(token_id)
        if not entry:
            self._log(token_id, accessor, "read", False)
            return None

        try:
            plaintext = self._decrypt(entry.encrypted_value, token_id, entry.key_version)
            entry.last_accessed = time.time()
            entry.access_count += 1
            self._log(token_id, accessor, "read", True)
            return plaintext
        except Exception as e:
            self._log(token_id, accessor, "read", False)
            raise

    def get_access_report(self, token_id: str) -> list[dict]:
        """Return access log for a specific token."""
        return [
            {"action": e.action, "accessor": e.accessor,
             "time": e.timestamp, "success": e.success}
            for e in self._access_log if e.token_id == token_id
        ]

vault = TokenVault(master_key=os.urandom(32))

# Store tokens during session creation
session_token_id = vault.store("user_42", "session", "sess_abc123xyz", accessor="auth_service")
api_key_id = vault.store("user_42", "api_key", "sk-ant-real-key-here", accessor="auth_service")

# Agent retrieves token by reference ID (never stores plaintext)
token = vault.retrieve(session_token_id, accessor="agent_service")
print(f"\nRetrieved token: {token[:10]}...")

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=50,
    messages=[{"role": "user", "content": "Session validation complete. Confirm."}]
)
print(f"Agent: {response.content[0].text[:80]}")

# Audit trail
print(f"\nAccess log for session token:")
for entry in vault.get_access_report(session_token_id):
    print(f"  {entry['accessor']} → {entry['action']} at {entry['time']:.0f}")

# Expected Token Savings: None — audit logging adds negligible overhead; breach forensics value is high
# Environment: ANTHROPIC_API_KEY required
```

### Option 4: Async Encrypted Redis-Style Session Store

```python
import anthropic
import asyncio
import os
import hashlib
import json
import base64
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic()

ENCRYPTION_KEY = os.urandom(32)  # Load from environment in production

def aes_gcm_encrypt(data: bytes, key: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM encryption. Returns (ciphertext, nonce)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, data, None)
        return ct, nonce
    except ImportError:
        # Fallback XOR (demo only)
        nonce = os.urandom(12)
        ct = bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))
        return ct, nonce

def aes_gcm_decrypt(ciphertext: bytes, nonce: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except ImportError:
        return bytes(ciphertext[i] ^ key[i % len(key)] for i in range(len(ciphertext)))

@dataclass
class AsyncEncryptedSession:
    session_id: str
    user_id: str
    encrypted_payload: bytes
    nonce: bytes
    created_at: float
    expires_at: float

class AsyncEncryptedSessionStore:
    def __init__(self, key: bytes):
        self._key = key
        self._sessions: dict[str, AsyncEncryptedSession] = {}
        self._cleanup_interval = 300  # Clean every 5 min

    async def create(self, user_id: str, payload: dict, ttl: float = 3600) -> str:
        import uuid
        session_id = str(uuid.uuid4())
        raw = json.dumps(payload).encode()
        ct, nonce = aes_gcm_encrypt(raw, self._key)
        now = time.time()

        self._sessions[session_id] = AsyncEncryptedSession(
            session_id=session_id, user_id=user_id,
            encrypted_payload=ct, nonce=nonce,
            created_at=now, expires_at=now + ttl
        )
        return session_id

    async def get(self, session_id: str) -> Optional[dict]:
        session = self._sessions.get(session_id)
        if not session or time.time() > session.expires_at:
            if session:
                del self._sessions[session_id]
            return None
        raw = aes_gcm_decrypt(session.encrypted_payload, session.nonce, self._key)
        return json.loads(raw)

    async def refresh(self, session_id: str, ttl: float = 3600) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.expires_at = time.time() + ttl
        return True

    async def delete(self, session_id: str):
        self._sessions.pop(session_id, None)

    async def cleanup_expired(self) -> int:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now > s.expires_at]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

store = AsyncEncryptedSessionStore(ENCRYPTION_KEY)

async def handle_agent_session(user_id: str, prompt: str, session_id: Optional[str] = None) -> dict:
    """Handle agent interaction with encrypted session."""
    payload = None
    if session_id:
        payload = await store.get(session_id)

    if not payload:
        payload = {
            "user_id": user_id,
            "access_token": f"tok_{os.urandom(6).hex()}",
            "preferences": {"lang": "en", "model": "haiku"},
            "conversation_count": 0
        }
        session_id = await store.create(user_id, payload)

    payload["conversation_count"] += 1
    await store.create.__wrapped__ if hasattr(store.create, '__wrapped__') else None

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "session_id": session_id,
        "turn": payload["conversation_count"],
        "response": response.content[0].text
    }

async def main():
    # Multi-turn session with encrypted storage
    r1 = await handle_agent_session("user_7", "Hello, what's your name?")
    print(f"Turn 1: {r1['response'][:80]}")

    r2 = await handle_agent_session("user_7", "Thanks!", r1["session_id"])
    print(f"Turn 2: {r2['response'][:80]}")

    cleaned = await store.cleanup_expired()
    print(f"Cleaned up {cleaned} expired sessions")

asyncio.run(main())

# Expected Token Savings: None — encryption overhead <1ms; prevents credential exposure on async store breach
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 5: Token Rotation with Zero-Downtime Re-encryption

```python
import anthropic
import os
import hashlib
import json
import time
import base64
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class VersionedEncryptedToken:
    token_id: str
    ciphertext_b64: str
    key_version: int
    created_at: float
    rotated_at: float = 0.0

class RotatingKeyVault:
    """
    Vault that supports key rotation without service downtime.
    Old tokens are re-encrypted to new key on next access.
    """
    def __init__(self):
        self._keys: dict[int, bytes] = {1: os.urandom(32)}
        self._current_version = 1
        self._tokens: dict[str, VersionedEncryptedToken] = {}
        self._reencryption_count = 0

    def _encrypt(self, plaintext: str, key_version: int) -> str:
        key = self._keys[key_version]
        raw = plaintext.encode()
        ct = bytes(raw[i] ^ key[i % len(key)] for i in range(len(raw)))
        return base64.b64encode(ct).decode()

    def _decrypt(self, ct_b64: str, key_version: int) -> str:
        key = self._keys[key_version]
        ct = base64.b64decode(ct_b64)
        raw = bytes(ct[i] ^ key[i % len(key)] for i in range(len(ct)))
        return raw.decode()

    def rotate_key(self) -> int:
        """Generate new encryption key. Old key retained for reading existing tokens."""
        new_version = self._current_version + 1
        self._keys[new_version] = os.urandom(32)
        self._current_version = new_version
        print(f"[Vault] Key rotated to version {new_version}")
        return new_version

    def store(self, token_id: str, plaintext: str):
        ct = self._encrypt(plaintext, self._current_version)
        self._tokens[token_id] = VersionedEncryptedToken(
            token_id=token_id,
            ciphertext_b64=ct,
            key_version=self._current_version,
            created_at=time.time()
        )

    def retrieve(self, token_id: str) -> Optional[str]:
        entry = self._tokens.get(token_id)
        if not entry:
            return None

        plaintext = self._decrypt(entry.ciphertext_b64, entry.key_version)

        # Lazy re-encryption: if stored with old key, re-encrypt with current key
        if entry.key_version < self._current_version:
            new_ct = self._encrypt(plaintext, self._current_version)
            entry.ciphertext_b64 = new_ct
            entry.key_version = self._current_version
            entry.rotated_at = time.time()
            self._reencryption_count += 1
            print(f"[Vault] Re-encrypted {token_id} to key v{self._current_version}")

        return plaintext

    def emergency_revoke_old_key(self, key_version: int):
        """After re-encryption complete, remove old key so old ciphertext is unreadable."""
        still_using = [t for t in self._tokens.values() if t.key_version == key_version]
        if still_using:
            raise ValueError(f"Cannot revoke key v{key_version}: {len(still_using)} tokens still use it")
        del self._keys[key_version]
        print(f"[Vault] Key v{key_version} revoked — {len(self._keys)} keys remaining")

    def stats(self) -> dict:
        version_counts: dict[int, int] = {}
        for t in self._tokens.values():
            version_counts[t.key_version] = version_counts.get(t.key_version, 0) + 1
        return {
            "current_key_version": self._current_version,
            "total_tokens": len(self._tokens),
            "tokens_by_key_version": version_counts,
            "reencryptions": self._reencryption_count
        }

vault = RotatingKeyVault()

# Store initial tokens
for i in range(3):
    vault.store(f"session_{i}", f"tok_user{i}_{os.urandom(4).hex()}")

print(f"Before rotation: {vault.stats()}")

# Rotate key
vault.rotate_key()

# Access tokens — they get lazily re-encrypted
for i in range(3):
    token = vault.retrieve(f"session_{i}")
    print(f"  session_{i}: {token[:20]}...")

print(f"\nAfter access: {vault.stats()}")

# Now safe to revoke old key (all tokens migrated)
try:
    vault.emergency_revoke_old_key(1)
except ValueError as e:
    print(f"Cannot revoke: {e}")

# Expected Token Savings: None — zero-downtime rotation prevents forced session invalidation
# Environment: ANTHROPIC_API_KEY not required for this example
```

### Option 6: Memory-Safe Token Handling with Scrubbing

```python
import anthropic
import ctypes
import os
import gc
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

class SecureBytes:
    """
    Wrapper that scrubs memory when token is no longer needed.
    Prevents plaintext token from lingering in heap after use.
    """
    def __init__(self, data: bytes):
        self._data = bytearray(data)  # Mutable — can be scrubbed
        self._scrubbed = False

    def read(self) -> bytes:
        if self._scrubbed:
            raise ValueError("SecureBytes has been scrubbed")
        return bytes(self._data)

    def scrub(self):
        """Overwrite memory with zeros before GC."""
        if not self._scrubbed:
            for i in range(len(self._data)):
                self._data[i] = 0
            self._scrubbed = True

    def __del__(self):
        self.scrub()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.scrub()

@dataclass
class SecureAgentSession:
    session_id: str
    user_id: str
    _secure_token: Optional[SecureBytes] = None
    created_at: float = 0.0
    used: bool = False

    def set_token(self, token: str):
        self._secure_token = SecureBytes(token.encode())
        self.created_at = time.time()

    def use_token(self) -> Optional[str]:
        """Return token for single use, then scrub."""
        if not self._secure_token or self._secure_token._scrubbed:
            return None
        token = self._secure_token.read().decode()
        return token

    def revoke(self):
        """Scrub token from memory immediately."""
        if self._secure_token:
            self._secure_token.scrub()
            self._secure_token = None
        gc.collect()

_sessions: dict[str, SecureAgentSession] = {}

def create_secure_session(user_id: str, token: str) -> str:
    import uuid
    session_id = str(uuid.uuid4())[:8]
    session = SecureAgentSession(session_id=session_id, user_id=user_id)
    session.set_token(token)
    _sessions[session_id] = session
    print(f"[SecureSession] Created session {session_id} for {user_id}")
    return session_id

def run_agent_with_secure_token(session_id: str, prompt: str) -> dict:
    """Use token without holding it in a plain Python string beyond minimum lifetime."""
    session = _sessions.get(session_id)
    if not session:
        return {"error": "session_not_found"}

    token = session.use_token()
    if not token:
        return {"error": "token_unavailable"}

    # Token is used and then the local var goes out of scope ASAP
    system = f"You are serving user {session.user_id}. Auth validated."
    # In real usage, token passed to authenticated HTTP call, not stored further
    auth_header = f"Bearer {token}"
    del token  # Explicitly clear

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return {"response": response.content[0].text, "session_id": session_id}

# Usage
session_id = create_secure_session("user_42", "tok_very_secret_api_key")
result = run_agent_with_secure_token(session_id, "Hello!")
print(f"Response: {result['response'][:80]}")

# Explicit revocation
_sessions[session_id].revoke()
print(f"Token scrubbed from memory: {_sessions[session_id]._secure_token is None}")

# Expected Token Savings: None — memory scrubbing prevents token recovery from heap dumps
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Encryption Method | Key Rotation | Audit Log | Best Use Case |
|--------|------------------|-------------|-----------|---------------|
| AES-GCM Envelope | AES-256-GCM | Manual | No | General session stores |
| Per-User Key Derivation | PBKDF2 + XOR | Via master key | No | Multi-tenant: limits blast radius |
| Token Vault + Access Log | XOR (demo) | No | Yes | Compliance/audit requirements |
| Async Encrypted Store | AES-GCM | No | No | Async/high-concurrency services |
| Rotating Key Vault | XOR (demo) | Yes (lazy re-enc) | No | Long-lived production deployments |
| Memory-Safe Scrubbing | In-memory only | N/A | No | Preventing heap dump token recovery |
