---
title: "Agent Doesn't Implement Credential Scope Minimization"
description: "AI agents that run all tool calls under a single admin-level credential violate the principle of least privilege—a compromised agent or prompt injection can exfiltrate data, delete resources, or escalate privileges far beyond what any single task requires. Credential scope minimization issues per-tool or per-operation scoped tokens with only the permissions required for that specific action, expiring immediately after use."
date: 2025-02-21
difficulty: advanced
category: security
slug: agent-doesnt-implement-credential-scope-minimization
tags:
  - least-privilege
  - credential-scoping
  - iam
  - security
  - token-scoping
  - principle-of-least-privilege
  - access-control
symptoms:
  - "Agent's database credential has INSERT, UPDATE, DELETE, DROP on all tables but only reads"
  - "A single compromised tool call could exfiltrate all user records via the admin API key"
  - "Audit logs show agent using admin credentials for simple read-only lookups"
  - "Prompt injection in a web-scraping tool could escalate to write operations via the same credential"
  - "Every agent instance shares one service account with full S3 bucket access"
---

## Problem

When an agent uses a single high-privilege credential for all tool calls, every tool becomes a potential escalation path. A prompt injection in a web-search result that convinces the agent to call a file-writing tool can now delete production data. A buggy tool that leaks its arguments exposes the master API key. Credential scope minimization assigns the narrowest possible permission set to each tool invocation: a read-only database token for SELECT queries, a write-only token for INSERT operations, an S3 token scoped to a single prefix for file uploads. Compromising any individual tool call yields only that operation's permissions, not the entire system.

---

## Solution 1: ScopedCredentialVault — Per-Operation Token Issuance

```python
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ScopedToken:
    credential: Any
    scope: str
    issued_at: float
    expires_at: float
    tool: str

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def ttl_seconds(self) -> float:
        return max(0.0, self.expires_at - time.time())


class ScopedCredentialVault:
    """
    Issues short-lived, narrowly-scoped credentials per tool call.
    Each tool declares its required scope; the vault mints a token
    with exactly that scope and a short TTL (default: 5 minutes).
    Tokens are cached for reuse within their TTL to avoid excessive
    token-exchange overhead.

    Usage:
        vault = ScopedCredentialVault(
            token_factory=sts_assume_role,   # scope -> credential
            default_ttl=300,
        )
        vault.register_tool("db_read", scope="db:read")
        vault.register_tool("db_write", scope="db:write")
        vault.register_tool("s3_upload", scope="s3:write:uploads/")

        cred = vault.get_credential("db_read")
        results = db_client.query(cred, sql)
    """

    def __init__(
        self,
        token_factory: Callable[[str], Any],
        default_ttl: float = 300.0,
    ):
        self._factory = token_factory
        self._default_ttl = default_ttl
        self._tool_scopes: Dict[str, str] = {}
        self._cache: Dict[str, ScopedToken] = {}

    def register_tool(self, tool_name: str, scope: str):
        self._tool_scopes[tool_name] = scope
        logger.info("tool_scope_registered tool=%s scope=%s", tool_name, scope)

    def get_credential(self, tool_name: str, ttl: Optional[float] = None) -> Any:
        scope = self._tool_scopes.get(tool_name)
        if scope is None:
            raise PermissionError(
                f"Tool '{tool_name}' has no registered scope — cannot issue credential"
            )

        cached = self._cache.get(scope)
        if cached and not cached.expired:
            logger.debug("scope_cache_hit scope=%s ttl_remaining=%.0fs",
                          scope, cached.ttl_seconds)
            return cached.credential

        ttl = ttl or self._default_ttl
        now = time.time()
        credential = self._factory(scope)
        token = ScopedToken(
            credential=credential,
            scope=scope,
            issued_at=now,
            expires_at=now + ttl,
            tool=tool_name,
        )
        self._cache[scope] = token
        logger.info("scoped_token_issued tool=%s scope=%s ttl=%.0fs", tool_name, scope, ttl)
        return credential

    def revoke(self, scope: str):
        self._cache.pop(scope, None)
        logger.info("scoped_token_revoked scope=%s", scope)

    def audit_summary(self) -> Dict[str, Any]:
        return {
            scope: {
                "tool": t.tool,
                "issued_at": t.issued_at,
                "expires_at": t.expires_at,
                "expired": t.expired,
            }
            for scope, t in self._cache.items()
        }
```

---

## Solution 2: ToolPermissionMatrix — Declare Allowed Operations Per Tool

```python
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolPermission:
    read: bool = False
    write: bool = False
    delete: bool = False
    admin: bool = False
    resources: FrozenSet[str] = field(default_factory=frozenset)  # allowed resource patterns

    def allows(self, operation: str, resource: str = "") -> bool:
        if operation == "read" and not self.read:
            return False
        if operation == "write" and not self.write:
            return False
        if operation == "delete" and not self.delete:
            return False
        if operation == "admin" and not self.admin:
            return False
        if self.resources and resource:
            return any(resource.startswith(r) for r in self.resources)
        return True


class ToolPermissionMatrix:
    """
    Declares the minimum required permissions for each tool and enforces
    that tool calls do not exceed their declared scope. Acts as a
    pre-execution gate: before invoking any tool, the matrix verifies
    the requested operation matches the tool's declared permission set.

    Usage:
        matrix = ToolPermissionMatrix()
        matrix.register("db_query", ToolPermission(read=True, resources=frozenset(["users.", "orders."])))
        matrix.register("db_insert", ToolPermission(write=True, resources=frozenset(["events."])))
        matrix.register("file_read", ToolPermission(read=True, resources=frozenset(["/data/"])))

        matrix.assert_allowed("db_query", operation="read", resource="users.email")
        matrix.assert_allowed("db_query", operation="write")  # raises PermissionError
    """

    def __init__(self):
        self._permissions: Dict[str, ToolPermission] = {}

    def register(self, tool_name: str, permission: ToolPermission):
        self._permissions[tool_name] = permission
        logger.info("tool_permission_registered tool=%s read=%s write=%s delete=%s",
                     tool_name, permission.read, permission.write, permission.delete)

    def get(self, tool_name: str) -> Optional[ToolPermission]:
        return self._permissions.get(tool_name)

    def check(self, tool_name: str, operation: str, resource: str = "") -> bool:
        perm = self._permissions.get(tool_name)
        if perm is None:
            logger.warning("permission_check_unknown_tool tool=%s", tool_name)
            return False
        allowed = perm.allows(operation, resource)
        if not allowed:
            logger.warning(
                "permission_denied tool=%s operation=%s resource=%s",
                tool_name, operation, resource,
            )
        return allowed

    def assert_allowed(self, tool_name: str, operation: str, resource: str = ""):
        if not self.check(tool_name, operation, resource):
            raise PermissionError(
                f"Tool '{tool_name}' attempted disallowed operation "
                f"'{operation}' on resource '{resource}'"
            )

    def summary(self) -> Dict[str, Dict]:
        return {
            name: {
                "read": p.read, "write": p.write,
                "delete": p.delete, "admin": p.admin,
                "resources": list(p.resources),
            }
            for name, p in self._permissions.items()
        }
```

---

## Solution 3: EphemeralTokenIssuer — Single-Use Tokens Per Tool Invocation

```python
import hashlib
import logging
import secrets
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class EphemeralTokenIssuer:
    """
    Issues a unique, single-use token for each individual tool invocation.
    The token is bound to a specific (tool_name, operation, request_id) triple
    and is invalidated immediately after first use or after a short TTL (60s).
    Prevents token replay across multiple tool calls even within the same session.

    Usage:
        issuer = EphemeralTokenIssuer(
            backend_credential_fn=get_aws_temp_creds,
            ttl=60,
        )
        token_id, token = issuer.issue("s3_upload", request_id="req-001")
        # Token is valid for one upload within 60 seconds
        s3_client.put_object(token=token, ...)
        issuer.consume(token_id)   # invalidate immediately after use
    """

    def __init__(
        self,
        backend_credential_fn: Callable[[str], Any],
        ttl: float = 60.0,
    ):
        self._backend = backend_credential_fn
        self._ttl = ttl
        self._issued: Dict[str, Dict] = {}  # token_id -> {credential, expires, used}

    def issue(self, tool_name: str, request_id: str = "") -> tuple:
        token_id = secrets.token_urlsafe(16)
        scope = f"tool:{tool_name}:req:{request_id}"
        credential = self._backend(scope)
        self._issued[token_id] = {
            "tool": tool_name,
            "scope": scope,
            "credential": credential,
            "issued_at": time.time(),
            "expires_at": time.time() + self._ttl,
            "used": False,
        }
        logger.info("ephemeral_token_issued tool=%s token_id=%s ttl=%.0f",
                     tool_name, token_id, self._ttl)
        return token_id, credential

    def consume(self, token_id: str) -> bool:
        """Mark token as used. Returns False if already used or expired."""
        record = self._issued.get(token_id)
        if record is None:
            logger.warning("ephemeral_token_unknown token_id=%s", token_id)
            return False
        if record["used"]:
            logger.warning("ephemeral_token_replay_attempt token_id=%s", token_id)
            return False
        if time.time() > record["expires_at"]:
            logger.warning("ephemeral_token_expired token_id=%s", token_id)
            self._issued.pop(token_id, None)
            return False
        record["used"] = True
        logger.info("ephemeral_token_consumed token_id=%s tool=%s", token_id, record["tool"])
        return True

    def cleanup_expired(self):
        now = time.time()
        stale = [tid for tid, r in self._issued.items() if r["expires_at"] < now]
        for tid in stale:
            self._issued.pop(tid)
```

---

## Solution 4: DatabaseScopeRouter — Per-Query Read/Write Connection Routing

```python
import logging
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DatabaseScopeRouter:
    """
    Routes database queries to different connection pools based on
    operation type. Read queries use a read-only replica with SELECT-only
    credentials; write queries use a write pool with INSERT/UPDATE but
    no DELETE; administrative queries (schema changes) require explicit
    elevation. Prevents a compromised read-path from executing writes.

    Usage:
        router = DatabaseScopeRouter(
            read_pool=create_pool(dsn_readonly),
            write_pool=create_pool(dsn_readwrite),
            admin_pool=create_pool(dsn_admin),
        )
        with router.read() as conn:
            rows = conn.execute("SELECT * FROM users WHERE id=?", [uid])
        with router.write() as conn:
            conn.execute("INSERT INTO events (type, data) VALUES (?,?)", [t, d])
    """

    def __init__(
        self,
        read_pool: Any,
        write_pool: Optional[Any] = None,
        admin_pool: Optional[Any] = None,
    ):
        self._read = read_pool
        self._write = write_pool
        self._admin = admin_pool

    @contextmanager
    def read(self):
        """Yields a read-only connection. SELECT only."""
        conn = self._read.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        """Yields a write connection. INSERT/UPDATE allowed, no DELETE/DROP."""
        if self._write is None:
            raise PermissionError("No write pool configured — write operations disallowed")
        conn = self._write.connect()
        logger.info("db_write_connection_acquired")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def admin(self, justification: str = ""):
        """Yields an admin connection. Requires explicit justification string."""
        if not justification:
            raise PermissionError(
                "Admin database access requires a justification string — "
                "pass justification='reason' to admin() context manager"
            )
        if self._admin is None:
            raise PermissionError("No admin pool configured")
        logger.warning("db_admin_access_acquired justification=%s", justification)
        conn = self._admin.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            logger.info("db_admin_connection_released")
```

---

## Solution 5: S3ScopedPresigner — Pre-Signed URLs with Narrow Object Scope

```python
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class S3ScopedPresigner:
    """
    Generates pre-signed S3 URLs scoped to a specific object key and
    HTTP method (GET or PUT). The agent never holds S3 credentials
    directly—it requests a pre-signed URL for exactly the object it needs
    to read or write, with a short expiry. Downstream tool calls (file
    upload, download) use the URL without needing any AWS credential.

    Usage:
        presigner = S3ScopedPresigner(
            bucket="agent-uploads",
            key_prefix="agent-outputs/",
            s3_client=boto3.client("s3"),
        )
        put_url = presigner.presign_put("result-001.json", expires_in=120)
        # Tool uploads directly to put_url via HTTP PUT — no AWS cred needed
        get_url = presigner.presign_get("result-001.json", expires_in=300)
    """

    def __init__(
        self,
        bucket: str,
        key_prefix: str,
        s3_client: object,
        max_object_size: int = 100 * 1024 * 1024,  # 100 MB
    ):
        self._bucket = bucket
        self._prefix = key_prefix.rstrip("/") + "/"
        self._s3 = s3_client
        self._max_size = max_object_size

    def _safe_key(self, object_name: str) -> str:
        # Strip any path traversal attempts
        safe = object_name.replace("..", "").lstrip("/")
        return self._prefix + safe

    def presign_put(self, object_name: str, expires_in: int = 120,
                     content_type: str = "application/octet-stream") -> str:
        key = self._safe_key(object_name)
        url = self._s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
        logger.info("s3_presigned_put key=%s expires_in=%d", key, expires_in)
        return url

    def presign_get(self, object_name: str, expires_in: int = 300) -> str:
        key = self._safe_key(object_name)
        url = self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        logger.info("s3_presigned_get key=%s expires_in=%d", key, expires_in)
        return url

    def presign_multipart(self, object_name: str,
                           parts: int = 10, expires_in: int = 600) -> List[str]:
        """Generate pre-signed URLs for each part of a multipart upload."""
        key = self._safe_key(object_name)
        resp = self._s3.create_multipart_upload(Bucket=self._bucket, Key=key)
        upload_id = resp["UploadId"]
        urls = []
        for part_num in range(1, parts + 1):
            url = self._s3.generate_presigned_url(
                "upload_part",
                Params={"Bucket": self._bucket, "Key": key,
                         "UploadId": upload_id, "PartNumber": part_num},
                ExpiresIn=expires_in,
            )
            urls.append(url)
        logger.info("s3_presigned_multipart key=%s parts=%d upload_id=%s",
                     key, parts, upload_id)
        return urls
```

---

## Solution 6: CredentialScopeAuditLog — Log Every Credential Issuance and Use

```python
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CredentialScopeAuditLog:
    """
    Records every credential issuance, use, and revocation as a structured
    audit event. Provides a queryable in-memory log for security review
    and emits to the structured logger for SIEM ingestion.
    Detects scope escalation attempts: a tool requesting a scope
    broader than its registered minimum.

    Usage:
        audit = CredentialScopeAuditLog(agent_id="agent-A")
        audit.log_issue(tool="db_read", scope="db:read:users", ttl=300)
        audit.log_use(tool="db_read", scope="db:read:users", success=True)
        report = audit.escalation_attempts()
    """

    def __init__(self, agent_id: str = "", max_records: int = 10_000):
        self._agent_id = agent_id
        self._max = max_records
        self._records: List[Dict[str, Any]] = []

    def _record(self, event: str, **fields):
        entry = {
            "event": f"credential_{event}",
            "agent_id": self._agent_id,
            "ts": time.time(),
            **fields,
        }
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(entry)
        logger.info(json.dumps(entry))

    def log_issue(self, tool: str, scope: str, ttl: float):
        self._record("issued", tool=tool, scope=scope, ttl=ttl)

    def log_use(self, tool: str, scope: str, success: bool, resource: str = ""):
        self._record("used", tool=tool, scope=scope, success=success, resource=resource)

    def log_revoke(self, scope: str, reason: str = ""):
        self._record("revoked", scope=scope, reason=reason)

    def log_denied(self, tool: str, requested_scope: str, allowed_scope: str):
        self._record(
            "denied",
            tool=tool,
            requested_scope=requested_scope,
            allowed_scope=allowed_scope,
        )

    def escalation_attempts(self) -> List[Dict]:
        return [r for r in self._records if r["event"] == "credential_denied"]

    def usage_by_tool(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in self._records:
            if r["event"] == "credential_used":
                counts[r.get("tool", "unknown")] = counts.get(r.get("tool", ""), 0) + 1
        return counts

    def summary(self) -> Dict[str, Any]:
        return {
            "total_records": len(self._records),
            "escalation_attempts": len(self.escalation_attempts()),
            "usage_by_tool": self.usage_by_tool(),
        }
```

---

## Comparison

| Approach | Scope Isolation | Token TTL | Single-Use | Resource Patterns | Audit Log | Integrated |
|---|---|---|---|---|---|---|
| **ScopedCredentialVault** | Per-tool | Yes | No | No | Basic | No |
| **ToolPermissionMatrix** | Per-operation | No | No | Yes | Warning only | No |
| **EphemeralTokenIssuer** | Per-invocation | Yes (60s) | Yes | No | Yes | No |
| **DatabaseScopeRouter** | Read/write/admin | N/A | No | No | Partial | No |
| **S3ScopedPresigner** | Per-object | Yes | No | Key prefix | Basic | No |
| **CredentialScopeAuditLog** | N/A | N/A | N/A | N/A | Yes | No |

**Key insight**: the minimum viable change is splitting credentials into two: one read-only credential for all lookup tools, one write-scoped credential for mutation tools, both with 1-hour TTLs. This immediately limits blast radius—a compromised read tool cannot write or delete. Layer `ToolPermissionMatrix` on top to enforce at the code level that `db_query` never receives a write credential even if a prompt injection attempts it. For the highest-sensitivity operations (billing, user PII exports), switch to `EphemeralTokenIssuer` for single-use tokens that expire in 60 seconds, making replay attacks and credential theft effectively useless.
