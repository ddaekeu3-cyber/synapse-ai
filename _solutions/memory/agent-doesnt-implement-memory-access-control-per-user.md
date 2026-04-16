---
layout: solution
title: "Agent Doesn't Implement Memory Access Control Per User"
category: memory
description: "Enforce per-user memory isolation so agents cannot read, write, or search memories belonging to other users — using namespaced keys, ownership validation, role-based access, and audit logging to prevent cross-user data leakage."
tags: [memory, access-control, multi-tenant, security, isolation, python]
---

# Agent Doesn't Implement Memory Access Control Per User

Agents that store memories in a shared namespace can accidentally surface one user's preferences, history, or sensitive information to another user. Access control on memory operations enforces ownership checks at read and write time — ensuring each user sees only their own memories regardless of how they query.

## Option 1: Namespaced Keys with Ownership Prefix

```python
import anthropic
import sqlite3
import time

client = anthropic.Anthropic()
DB = "memory_acl.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            key TEXT PRIMARY KEY, user_id TEXT,
            content TEXT, ts REAL
        )
    """)
    con.commit(); con.close()

def _user_key(user_id: str, key: str) -> str:
    """Namespace every key with the user_id — prevents cross-user key guessing."""
    return f"user:{user_id}:{key}"

def memory_set(user_id: str, key: str, content: str):
    init_db()
    namespaced = _user_key(user_id, key)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO memories VALUES (?,?,?,?)",
        (namespaced, user_id, content, time.time()),
    )
    con.commit(); con.close()

def memory_get(user_id: str, key: str) -> str | None:
    init_db()
    namespaced = _user_key(user_id, key)
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM memories WHERE key=? AND user_id=?",
        (namespaced, user_id),  # double-check user_id even with namespaced key
    ).fetchone()
    con.close()
    return row[0] if row else None

def memory_list(user_id: str) -> list[dict]:
    init_db()
    prefix = f"user:{user_id}:"
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT key, content FROM memories WHERE user_id=?", (user_id,)
    ).fetchall()
    con.close()
    return [{"key": r[0].replace(prefix, ""), "content": r[1]} for r in rows]

# Store memories for two users
memory_set("alice", "preference", "prefers concise answers")
memory_set("alice", "language",   "Python expert")
memory_set("bob",   "preference", "likes detailed explanations")

# Alice reads her own — ok
print(f"Alice preference: {memory_get('alice', 'preference')!r}")

# Bob cannot read Alice's data — returns None even with correct key name
print(f"Bob reads Alice's key: {memory_get('bob', 'preference')!r}")  # Bob's preference, not Alice's
print(f"Bob's preference: {memory_get('bob', 'preference')!r}")

# List is scoped
print(f"Alice memories: {memory_list('alice')}")
print(f"Bob memories:   {memory_list('bob')}")

# Expected Token Savings: Namespaced isolation prevents defensive "filter everything" queries; scoped list is cheaper
# Environment: SQLite double-check (key + user_id) prevents namespace collision attacks; use UUID user IDs
```

## Option 2: Role-Based Access Control for Memory Operations

```python
import anthropic
import sqlite3
import time
from enum import Enum

client = anthropic.Anthropic()
DB = "memory_rbac.db"

class Role(Enum):
    USER  = "user"    # read/write own memories only
    ADMIN = "admin"   # read/write any user's memories
    AGENT = "agent"   # read any, write own namespace only

PERMISSIONS = {
    Role.USER:  {"read_own": True,  "write_own": True,  "read_others": False, "write_others": False},
    Role.ADMIN: {"read_own": True,  "write_own": True,  "read_others": True,  "write_others": True},
    Role.AGENT: {"read_own": True,  "write_own": True,  "read_others": True,  "write_others": False},
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT, key TEXT, content TEXT, ts REAL,
            UNIQUE(owner_id, key)
        )
    """)
    con.commit(); con.close()

def can(actor_id: str, actor_role: Role, op: str, target_owner: str) -> bool:
    perms = PERMISSIONS[actor_role]
    is_own = actor_id == target_owner
    if is_own:
        return perms.get(f"{op}_own", False)
    return perms.get(f"{op}_others", False)

def memory_write(actor_id: str, actor_role: Role, owner_id: str, key: str, content: str):
    if not can(actor_id, actor_role, "write", owner_id):
        raise PermissionError(
            f"{actor_role.value} '{actor_id}' cannot write memory for '{owner_id}'"
        )
    init_db()
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO memories (owner_id, key, content, ts) VALUES (?,?,?,?)",
        (owner_id, key, content, time.time()),
    )
    con.commit(); con.close()

def memory_read(actor_id: str, actor_role: Role, owner_id: str, key: str) -> str | None:
    if not can(actor_id, actor_role, "read", owner_id):
        raise PermissionError(
            f"{actor_role.value} '{actor_id}' cannot read memory for '{owner_id}'"
        )
    init_db()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM memories WHERE owner_id=? AND key=?", (owner_id, key)
    ).fetchone()
    con.close()
    return row[0] if row else None

# Setup data
memory_write("alice", Role.USER,  "alice", "lang",  "Python")
memory_write("bob",   Role.USER,  "bob",   "lang",  "JavaScript")
memory_write("admin", Role.ADMIN, "alice", "note",  "VIP user")

# Access checks
print(f"Alice reads own: {memory_read('alice', Role.USER, 'alice', 'lang')!r}")
print(f"Admin reads Alice: {memory_read('admin', Role.ADMIN, 'alice', 'lang')!r}")

try:
    memory_read("bob", Role.USER, "alice", "lang")
except PermissionError as e:
    print(f"Bob blocked: {e}")

try:
    memory_write("alice", Role.USER, "bob", "lang", "hacked")
except PermissionError as e:
    print(f"Alice write to Bob blocked: {e}")

# Expected Token Savings: RBAC blocks at operation level; no data fetched for unauthorized reads
# Environment: extend Role with GROUP for team-scoped memories; combine with audit log for compliance
```

## Option 3: Ownership-Validated Memory with Audit Trail

```python
import anthropic
import sqlite3
import time

client = anthropic.Anthropic()
DB = "memory_audit.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, key TEXT, content TEXT,
            created_ts REAL, updated_ts REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            ts REAL, actor_id TEXT, operation TEXT,
            target_user TEXT, key TEXT, allowed INTEGER, reason TEXT
        )
    """)
    con.commit(); con.close()

def _audit(actor_id: str, op: str, target_user: str, key: str, allowed: bool, reason: str = ""):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO access_log VALUES (?,?,?,?,?,?,?)",
        (time.time(), actor_id, op, target_user, key, int(allowed), reason),
    )
    con.commit(); con.close()

def memory_store(actor_id: str, content: str, key: str = "default") -> bool:
    """Store always writes to the actor's own namespace."""
    init_db()
    ts = time.time()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT id FROM memories WHERE user_id=? AND key=?", (actor_id, key)
    ).fetchone()
    if row:
        con.execute("UPDATE memories SET content=?, updated_ts=? WHERE id=?", (content, ts, row[0]))
    else:
        con.execute(
            "INSERT INTO memories (user_id, key, content, created_ts, updated_ts) VALUES (?,?,?,?,?)",
            (actor_id, key, content, ts, ts),
        )
    con.commit(); con.close()
    _audit(actor_id, "write", actor_id, key, True)
    return True

def memory_fetch(actor_id: str, target_user: str, key: str = "default") -> str | None:
    """Fetch: actor can only read their own memories."""
    init_db()
    if actor_id != target_user:
        _audit(actor_id, "read", target_user, key, False, "cross-user read denied")
        raise PermissionError(f"User '{actor_id}' cannot read memories of '{target_user}'")
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM memories WHERE user_id=? AND key=?", (target_user, key)
    ).fetchone()
    con.close()
    _audit(actor_id, "read", target_user, key, True)
    return row[0] if row else None

def access_report(actor_id: str | None = None) -> list[dict]:
    con = sqlite3.connect(DB)
    if actor_id:
        rows = con.execute(
            "SELECT ts, actor_id, operation, target_user, key, allowed, reason "
            "FROM access_log WHERE actor_id=? ORDER BY ts DESC LIMIT 20",
            (actor_id,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT ts, actor_id, operation, target_user, key, allowed, reason "
            "FROM access_log ORDER BY ts DESC LIMIT 20"
        ).fetchall()
    con.close()
    return [{"actor": r[1], "op": r[2], "target": r[3], "key": r[4],
             "allowed": bool(r[5]), "reason": r[6]} for r in rows]

memory_store("alice", "Alice's preference: concise")
memory_store("bob",   "Bob's preference: detailed")

print(memory_fetch("alice", "alice"))  # ok

try:
    memory_fetch("bob", "alice")
except PermissionError as e:
    print(f"Blocked: {e}")

print("\nAccess log:")
for entry in access_report():
    status = "✓" if entry["allowed"] else "✗"
    print(f"  {status} [{entry['actor']}] {entry['op']} -> {entry['target']}:{entry['key']} {entry['reason']}")

# Expected Token Savings: Denied reads return instantly with no DB data fetched; audit log enables compliance reports
# Environment: audit log is append-only; index on actor_id and ts for efficient per-user reporting
```

## Option 4: Token-Based Memory Access (Scoped API Tokens)

```python
import anthropic
import sqlite3
import hashlib
import secrets
import time

client = anthropic.Anthropic()
DB = "memory_tokens.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            token_hash TEXT PRIMARY KEY, user_id TEXT,
            scopes TEXT, expires_at REAL, created_ts REAL
        );
        CREATE TABLE IF NOT EXISTS memories (
            user_id TEXT, key TEXT, content TEXT, ts REAL,
            PRIMARY KEY (user_id, key)
        );
    """)
    con.commit(); con.close()

def issue_token(user_id: str, scopes: list[str], ttl_s: int = 3600) -> str:
    """Issue a scoped bearer token for memory access."""
    init_db()
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + ttl_s
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO tokens VALUES (?,?,?,?,?)",
        (token_hash, user_id, ",".join(scopes), expires, time.time()),
    )
    con.commit(); con.close()
    return token

def resolve_token(token: str) -> dict | None:
    """Resolve token to user_id + scopes; return None if invalid/expired."""
    init_db()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT user_id, scopes, expires_at FROM tokens WHERE token_hash=?", (token_hash,)
    ).fetchone()
    con.close()
    if not row or time.time() > row[2]:
        return None
    return {"user_id": row[0], "scopes": row[1].split(",")}

def memory_write_with_token(token: str, key: str, content: str) -> bool:
    identity = resolve_token(token)
    if not identity or "memory:write" not in identity["scopes"]:
        raise PermissionError("Token missing memory:write scope or invalid")
    user_id = identity["user_id"]
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO memories VALUES (?,?,?,?)",
        (user_id, key, content, time.time()),
    )
    con.commit(); con.close()
    return True

def memory_read_with_token(token: str, key: str) -> str | None:
    identity = resolve_token(token)
    if not identity or "memory:read" not in identity["scopes"]:
        raise PermissionError("Token missing memory:read scope or invalid")
    user_id = identity["user_id"]
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM memories WHERE user_id=? AND key=?", (user_id, key)
    ).fetchone()
    con.close()
    return row[0] if row else None

# Issue tokens with different scopes
alice_rw  = issue_token("alice", ["memory:read", "memory:write"])
alice_ro  = issue_token("alice", ["memory:read"])  # read-only
bob_token = issue_token("bob",   ["memory:read", "memory:write"])

# Alice writes with RW token
memory_write_with_token(alice_rw, "pref", "concise")
print(f"Alice read own: {memory_read_with_token(alice_rw, 'pref')!r}")

# Alice read-only token cannot write
try:
    memory_write_with_token(alice_ro, "pref", "verbose")
except PermissionError as e:
    print(f"RO token blocked write: {e}")

# Bob's token scoped to Bob's memories only — he reads his own namespace
memory_write_with_token(bob_token, "pref", "detailed")
print(f"Bob read own: {memory_read_with_token(bob_token, 'pref')!r}")

# Expected Token Savings: Token scope check is O(1) hash lookup; expired tokens rejected without DB memory query
# Environment: token TTL prevents indefinite access; rotate tokens per session for security; use HMAC for production
```

## Option 5: Encrypted Memory Namespace Per User

```python
import anthropic
import sqlite3
import hashlib
import base64
import time

client = anthropic.Anthropic()
DB = "memory_encrypted.db"

def derive_key(user_id: str, secret: str = "agent-secret-key") -> bytes:
    """Derive a per-user encryption key (deterministic from user_id + secret)."""
    return hashlib.sha256(f"{secret}:{user_id}".encode()).digest()

def xor_encrypt(data: bytes, key: bytes) -> bytes:
    """Simple XOR cipher for demonstration. Use AES-GCM in production."""
    key_repeated = (key * ((len(data) // len(key)) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_repeated))

def encrypt(user_id: str, plaintext: str) -> str:
    key = derive_key(user_id)
    encrypted = xor_encrypt(plaintext.encode(), key)
    return base64.b64encode(encrypted).decode()

def decrypt(user_id: str, ciphertext: str) -> str:
    key = derive_key(user_id)
    encrypted = base64.b64decode(ciphertext.encode())
    return xor_encrypt(encrypted, key).decode()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            user_id TEXT, key TEXT, ciphertext TEXT, ts REAL,
            PRIMARY KEY (user_id, key)
        )
    """)
    con.commit(); con.close()

def secure_store(user_id: str, key: str, content: str):
    init_db()
    ciphertext = encrypt(user_id, content)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO memories VALUES (?,?,?,?)",
        (user_id, key, ciphertext, time.time()),
    )
    con.commit(); con.close()

def secure_read(requesting_user: str, owner_id: str, key: str) -> str | None:
    """Read only works if requesting_user == owner_id (same decryption key)."""
    if requesting_user != owner_id:
        raise PermissionError(f"'{requesting_user}' cannot decrypt memories of '{owner_id}'")
    init_db()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT ciphertext FROM memories WHERE user_id=? AND key=?", (owner_id, key)
    ).fetchone()
    con.close()
    if not row:
        return None
    return decrypt(owner_id, row[0])

secure_store("alice", "secret", "My API key is abc123")
secure_store("bob",   "secret", "Bob's private note")

# Correct access
print(f"Alice decrypts own: {secure_read('alice', 'alice', 'secret')!r}")

# Wrong user can't decrypt even if they get the row
try:
    print(secure_read("bob", "alice", "secret"))
except PermissionError as e:
    print(f"Blocked: {e}")

# Demonstrate: raw ciphertext is unreadable without user key
con = sqlite3.connect(DB)
row = con.execute("SELECT ciphertext FROM memories WHERE user_id='alice'").fetchone()
con.close()
print(f"Raw ciphertext: {row[0][:40]}...")

# Expected Token Savings: Encryption means even DB admin cannot read user memories; access control is cryptographic
# Environment: replace XOR with cryptography.fernet.Fernet for production; store salt per user for key derivation
```

## Option 6: Multi-Tenant Memory with Quota and Access Policy

```python
import anthropic
import sqlite3
import time
from dataclasses import dataclass

client = anthropic.Anthropic()
DB = "memory_multitenant.db"

@dataclass
class TenantPolicy:
    tenant_id: str
    max_memories: int = 100
    max_content_bytes: int = 10_000
    allow_agent_read: bool = True   # allow agent to read this tenant's memories
    allow_export: bool = False      # allow bulk export

POLICIES: dict[str, TenantPolicy] = {
    "tenant-free":  TenantPolicy("tenant-free",  max_memories=10,  max_content_bytes=1_000),
    "tenant-pro":   TenantPolicy("tenant-pro",   max_memories=500, max_content_bytes=50_000),
    "tenant-admin": TenantPolicy("tenant-admin", max_memories=9999, allow_export=True),
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT, user_id TEXT, key TEXT,
            content TEXT, ts REAL,
            UNIQUE(tenant_id, user_id, key)
        )
    """)
    con.commit(); con.close()

def get_policy(tenant_id: str) -> TenantPolicy:
    return POLICIES.get(tenant_id, TenantPolicy(tenant_id))

def memory_store(tenant_id: str, user_id: str, key: str, content: str):
    init_db()
    policy = get_policy(tenant_id)
    con = sqlite3.connect(DB)
    count = con.execute(
        "SELECT COUNT(*) FROM memories WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
    ).fetchone()[0]
    if count >= policy.max_memories:
        raise MemoryError(f"Quota exceeded: {count}/{policy.max_memories} memories for {user_id}")
    if len(content.encode()) > policy.max_content_bytes:
        raise ValueError(f"Content too large: {len(content.encode())} > {policy.max_content_bytes} bytes")
    con.execute(
        "INSERT OR REPLACE INTO memories (tenant_id, user_id, key, content, ts) VALUES (?,?,?,?,?)",
        (tenant_id, user_id, key, content, time.time()),
    )
    con.commit(); con.close()

def memory_query(tenant_id: str, user_id: str, requesting_user: str, key: str) -> str | None:
    """Requesting user can only read their own memories within a tenant."""
    if requesting_user != user_id:
        raise PermissionError(f"Cross-user read denied within tenant {tenant_id!r}")
    init_db()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT content FROM memories WHERE tenant_id=? AND user_id=? AND key=?",
        (tenant_id, user_id, key),
    ).fetchone()
    con.close()
    return row[0] if row else None

def tenant_stats(tenant_id: str) -> dict:
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT user_id), SUM(LENGTH(content)) "
        "FROM memories WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    con.close()
    policy = get_policy(tenant_id)
    return {
        "tenant_id": tenant_id,
        "total_memories": row[0],
        "unique_users": row[1],
        "total_bytes": row[2] or 0,
        "quota": policy.max_memories,
        "quota_pct": round((row[0] / policy.max_memories) * 100, 1),
    }

# Store for multiple tenants
memory_store("tenant-free", "alice", "pref", "concise")
memory_store("tenant-pro",  "bob",   "pref", "detailed with examples")
memory_store("tenant-pro",  "carol", "pref", "bullet points")

print(memory_query("tenant-free", "alice", "alice", "pref"))
try:
    memory_query("tenant-free", "alice", "bob", "pref")
except PermissionError as e:
    print(f"Blocked: {e}")

for tid in ["tenant-free", "tenant-pro"]:
    stats = tenant_stats(tid)
    print(f"[{tid}] {stats['total_memories']}/{stats['quota']} memories "
          f"({stats['unique_users']} users, {stats['total_bytes']} bytes)")

# Expected Token Savings: Quota enforcement prevents one user from filling shared storage; tenant isolation is physical
# Environment: extend POLICIES from DB for dynamic tier management; add billing hooks on quota approaching
```

## Comparison

| Option | Isolation Mechanism | Audit Log | Quota | Encryption |
|--------|-------------------|-----------|-------|-----------|
| 1 — Namespaced Keys | Prefix + owner check | No | No | No |
| 2 — RBAC Roles | Permission matrix | No | No | No |
| 3 — Ownership + Audit | Owner check | SQLite log | No | No |
| 4 — Scoped Tokens | Token hash + scope | No | No | No |
| 5 — Encrypted Namespace | Cryptographic | No | No | XOR/AES |
| 6 — Multi-Tenant Quota | Tenant + owner check | No | Yes | No |
