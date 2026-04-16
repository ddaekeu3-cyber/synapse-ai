---
title: "Agent Doesn't Implement Secure Credential Passing Between Agents"
description: "Multi-agent pipelines that pass credentials as plaintext in tool arguments or shared memory expose secrets to every agent in the chain, prompt injection attacks, and logging systems. Implement secure credential delegation using short-lived scoped tokens, encrypted handoff envelopes, and credential brokers that issue agent-specific credentials without exposing the underlying secret."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-secure-credential-passing-between-agents
tags: [credential-delegation, multi-agent, secrets-management, token-scoping, secure-handoff, security]
symptoms:
  - "API keys appear in tool call arguments visible in conversation history and logs"
  - "Subagent receives full admin credentials when it only needs read access to one resource"
  - "Credentials passed in shared memory can be read by any agent with memory access"
  - "No credential expiry — a compromised subagent retains credentials indefinitely"
  - "Prompt injection in user input could cause the agent to log or echo its own credentials"
---

## Why This Happens

Agents in a pipeline often need to call downstream APIs on behalf of the user. The naive approach is to pass the API key as a tool argument or environment variable, which means it appears in tool call logs, prompt history, and any memory snapshot. Secure credential delegation separates the credential (which only the broker knows) from the proof-of-authorization (a short-lived scoped token that the subagent presents). Even if the token is leaked, it expires shortly and is scoped to the minimum required permissions.

## Solution 1: Scoped Credential Token

```python
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

@dataclass
class ScopedCredentialToken:
    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    issuer_agent_id: str = ""
    recipient_agent_id: str = ""
    credential_ref: str = ""        # opaque reference — never the credential itself
    allowed_scopes: List[str] = field(default_factory=list)   # e.g. ["read:users", "write:events"]
    allowed_resources: List[str] = field(default_factory=list)  # specific resource IDs
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    max_uses: int = 0               # 0 = unlimited within TTL
    use_count: int = 0
    revoked: bool = False
    signature: str = ""

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if time.time() > self.expires_at:
            return False
        if self.max_uses > 0 and self.use_count >= self.max_uses:
            return False
        return True

    def has_scope(self, required_scope: str) -> bool:
        return required_scope in self.allowed_scopes

    def to_safe_dict(self) -> dict:
        """Returns token metadata safe to log — never includes the credential."""
        return {
            "token_id": self.token_id,
            "issuer": self.issuer_agent_id,
            "recipient": self.recipient_agent_id,
            "scopes": self.allowed_scopes,
            "expires_at": self.expires_at,
            "use_count": self.use_count,
            "valid": self.is_valid(),
        }
```

## Solution 2: Credential Broker

```python
import hashlib
import hmac
import secrets
import time
from typing import Dict, List, Optional

class AgentCredentialBroker:
    """
    Central broker that holds actual credentials and issues scoped tokens.
    Agents never see the underlying credential — they only present tokens.
    The broker resolves tokens to credentials at call time and makes the
    API call itself, or issues a short-lived exchange token to a trusted caller.
    """

    def __init__(self, signing_secret: bytes):
        self._secret = signing_secret
        self._credentials: Dict[str, str] = {}       # ref -> actual credential
        self._tokens: Dict[str, ScopedCredentialToken] = {}
        self._revoked_tokens: set = set()
        self._issued_count = 0
        self._resolved_count = 0

    def register_credential(self, credential_ref: str, credential_value: str) -> None:
        """Store a credential under an opaque reference. Never log credential_value."""
        self._credentials[credential_ref] = credential_value

    def _sign_token(self, token: ScopedCredentialToken) -> str:
        payload = (
            f"{token.token_id}:{token.recipient_agent_id}:"
            f"{token.credential_ref}:{token.expires_at}"
        ).encode()
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _verify_signature(self, token: ScopedCredentialToken) -> bool:
        expected = self._sign_token(token)
        return hmac.compare_digest(token.signature, expected)

    def issue_token(
        self,
        issuer_agent_id: str,
        recipient_agent_id: str,
        credential_ref: str,
        scopes: List[str],
        ttl_seconds: float = 300.0,
        max_uses: int = 10,
        allowed_resources: List[str] = None,
    ) -> Optional[ScopedCredentialToken]:
        if credential_ref not in self._credentials:
            return None

        token = ScopedCredentialToken(
            issuer_agent_id=issuer_agent_id,
            recipient_agent_id=recipient_agent_id,
            credential_ref=credential_ref,
            allowed_scopes=scopes,
            allowed_resources=allowed_resources or [],
            expires_at=time.time() + ttl_seconds,
            max_uses=max_uses,
        )
        token.signature = self._sign_token(token)
        self._tokens[token.token_id] = token
        self._issued_count += 1
        return token

    def resolve(
        self,
        token_id: str,
        requesting_agent_id: str,
        required_scope: str,
        resource_id: str = "",
    ) -> Optional[str]:
        """
        Resolves a token to the actual credential.
        Returns None if the token is invalid, expired, or missing required scope.
        """
        token = self._tokens.get(token_id)
        if not token:
            return None
        if not self._verify_signature(token):
            return None
        if token.recipient_agent_id != requesting_agent_id:
            return None
        if not token.is_valid():
            return None
        if not token.has_scope(required_scope):
            return None
        if resource_id and token.allowed_resources and resource_id not in token.allowed_resources:
            return None

        token.use_count += 1
        self._resolved_count += 1
        return self._credentials.get(token.credential_ref)

    def revoke(self, token_id: str) -> bool:
        token = self._tokens.get(token_id)
        if token:
            token.revoked = True
            self._revoked_tokens.add(token_id)
            return True
        return False

    def stats(self) -> dict:
        return {
            "registered_credentials": len(self._credentials),
            "active_tokens": sum(1 for t in self._tokens.values() if t.is_valid()),
            "issued_total": self._issued_count,
            "resolved_total": self._resolved_count,
        }
```

## Solution 3: Encrypted Credential Envelope

```python
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class CredentialEnvelope:
    """
    Encrypted wrapper for passing a credential between trusted agents.
    Uses symmetric encryption (Fernet/AES-GCM) so only an agent with
    the shared key can decrypt the payload.
    Requires: cryptography library.
    """
    ciphertext_b64: str
    recipient_agent_id: str
    expires_at: float
    envelope_id: str

class CredentialEnvelopeFactory:
    """
    Creates and opens encrypted credential envelopes.
    The shared_key is pre-distributed to trusted agents via secure channel.
    Each envelope is single-use and expires after TTL.
    """

    def __init__(self, shared_key: bytes):
        try:
            from cryptography.fernet import Fernet
            import base64
            # Fernet requires 32 bytes encoded as urlsafe base64
            key_b64 = base64.urlsafe_b64encode(shared_key[:32].ljust(32, b'\x00'))
            self._fernet = Fernet(key_b64)
        except ImportError:
            self._fernet = None
        self._opened: set = set()   # track opened envelopes for single-use enforcement

    def seal(
        self,
        credential_value: str,
        recipient_agent_id: str,
        ttl_seconds: float = 60.0,
    ) -> Optional[CredentialEnvelope]:
        if not self._fernet:
            return None
        import uuid
        payload = json.dumps({
            "credential": credential_value,
            "recipient": recipient_agent_id,
            "expires_at": time.time() + ttl_seconds,
            "nonce": os.urandom(16).hex(),
        }).encode()
        ciphertext = self._fernet.encrypt(payload)
        return CredentialEnvelope(
            ciphertext_b64=ciphertext.decode(),
            recipient_agent_id=recipient_agent_id,
            expires_at=time.time() + ttl_seconds,
            envelope_id=str(uuid.uuid4())[:8],
        )

    def open(
        self,
        envelope: CredentialEnvelope,
        opening_agent_id: str,
    ) -> Optional[str]:
        if not self._fernet:
            return None
        if envelope.envelope_id in self._opened:
            return None   # single-use
        if time.time() > envelope.expires_at:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(envelope.ciphertext_b64.encode()))
        except Exception:
            return None
        if payload.get("recipient") != opening_agent_id:
            return None
        if time.time() > payload.get("expires_at", 0):
            return None
        self._opened.add(envelope.envelope_id)
        return payload.get("credential")
```

## Solution 4: Credential Audit Logger

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class CredentialAuditEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_type: str = ""    # "token_issued" | "token_resolved" | "token_denied" | "token_revoked"
    token_id: str = ""
    issuer_agent_id: str = ""
    recipient_agent_id: str = ""
    credential_ref: str = ""     # opaque ref only — never the value
    scope_requested: str = ""
    resource_id: str = ""
    success: bool = True
    denial_reason: str = ""
    timestamp: float = field(default_factory=time.time)

class CredentialAuditLogger:
    """
    Append-only audit log for all credential operations.
    Logs token issuance, resolution, denial, and revocation.
    Never logs actual credential values — only opaque references.
    """

    def __init__(self, max_entries: int = 50_000):
        self._entries: List[CredentialAuditEntry] = []
        self._max = max_entries

    def log_issue(self, token: ScopedCredentialToken) -> None:
        self._append(CredentialAuditEntry(
            event_type="token_issued",
            token_id=token.token_id,
            issuer_agent_id=token.issuer_agent_id,
            recipient_agent_id=token.recipient_agent_id,
            credential_ref=token.credential_ref,
        ))

    def log_resolve(
        self,
        token_id: str,
        agent_id: str,
        scope: str,
        resource_id: str,
        success: bool,
        reason: str = "",
    ) -> None:
        self._append(CredentialAuditEntry(
            event_type="token_resolved" if success else "token_denied",
            token_id=token_id,
            recipient_agent_id=agent_id,
            scope_requested=scope,
            resource_id=resource_id,
            success=success,
            denial_reason=reason,
        ))

    def log_revoke(self, token_id: str, revoked_by: str) -> None:
        self._append(CredentialAuditEntry(
            event_type="token_revoked",
            token_id=token_id,
            issuer_agent_id=revoked_by,
        ))

    def _append(self, entry: CredentialAuditEntry) -> None:
        if len(self._entries) >= self._max:
            self._entries.pop(0)
        self._entries.append(entry)

    def denied_attempts(self, since_seconds: float = 3600.0) -> List[CredentialAuditEntry]:
        cutoff = time.time() - since_seconds
        return [
            e for e in self._entries
            if e.event_type == "token_denied" and e.timestamp >= cutoff
        ]

    def summary(self) -> dict:
        total = len(self._entries)
        return {
            "total_events": total,
            "issued": sum(1 for e in self._entries if e.event_type == "token_issued"),
            "resolved": sum(1 for e in self._entries if e.event_type == "token_resolved"),
            "denied": sum(1 for e in self._entries if e.event_type == "token_denied"),
            "revoked": sum(1 for e in self._entries if e.event_type == "token_revoked"),
        }
```

## Solution 5: Least-Privilege Token Minter

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

@dataclass
class ToolPermissionPolicy:
    tool_name: str
    required_scopes: List[str]
    allowed_resources: List[str] = field(default_factory=list)
    max_ttl_seconds: float = 300.0
    max_uses: int = 5

class LeastPrivilegeTokenMinter:
    """
    Mints the minimum-scoped token required for a specific tool call.
    Given the tool name and target resources, issues a token with
    only the scopes that tool actually needs — never more.
    Prevents privilege escalation through over-provisioned tokens.
    """

    def __init__(
        self,
        broker: AgentCredentialBroker,
        policies: List[ToolPermissionPolicy] = None,
    ):
        self._broker = broker
        self._policies: Dict[str, ToolPermissionPolicy] = {
            p.tool_name: p for p in (policies or [])
        }

    def register_policy(self, policy: ToolPermissionPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def mint_for_tool(
        self,
        tool_name: str,
        credential_ref: str,
        issuer_agent_id: str,
        recipient_agent_id: str,
        requested_resources: List[str] = None,
    ) -> Optional[ScopedCredentialToken]:
        policy = self._policies.get(tool_name)
        if not policy:
            return None   # no policy = no token

        # Intersect requested resources with policy-allowed resources
        if policy.allowed_resources:
            resources = [
                r for r in (requested_resources or [])
                if r in policy.allowed_resources
            ]
            if not resources and requested_resources:
                return None   # requested resources not allowed by policy
        else:
            resources = requested_resources or []

        return self._broker.issue_token(
            issuer_agent_id=issuer_agent_id,
            recipient_agent_id=recipient_agent_id,
            credential_ref=credential_ref,
            scopes=policy.required_scopes,
            ttl_seconds=policy.max_ttl_seconds,
            max_uses=policy.max_uses,
            allowed_resources=resources,
        )
```

## Solution 6: Credential Leak Detector

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class LeakFinding:
    field_path: str
    pattern_name: str
    snippet: str    # first 20 chars of match, not the full secret

CREDENTIAL_PATTERNS = [
    ("api_key_generic",   re.compile(r"['\"]?api[_-]?key['\"]?\s*[=:]\s*['\"]?([A-Za-z0-9_\-]{20,})", re.I)),
    ("bearer_token",      re.compile(r"Bearer\s+([A-Za-z0-9_\-\.]{20,})", re.I)),
    ("aws_access_key",    re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_pem",   re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
    ("password_field",    re.compile(r"['\"]?password['\"]?\s*[=:]\s*['\"]?([^\s'\"]{8,})", re.I)),
    ("sk_secret_key",     re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token",      re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
]

class CredentialLeakDetector:
    """
    Scans tool arguments, tool results, and agent messages for
    credential patterns before they are logged or passed to subagents.
    Raises an alert on detection; optionally redacts in-place.
    """

    def scan(self, data: Any, path: str = "") -> List[LeakFinding]:
        findings = []
        if isinstance(data, str):
            for name, pattern in CREDENTIAL_PATTERNS:
                match = pattern.search(data)
                if match:
                    findings.append(LeakFinding(
                        field_path=path,
                        pattern_name=name,
                        snippet=match.group(0)[:20] + "...",
                    ))
        elif isinstance(data, dict):
            for key, value in data.items():
                findings.extend(self.scan(value, path=f"{path}.{key}" if path else key))
        elif isinstance(data, list):
            for i, item in enumerate(data):
                findings.extend(self.scan(item, path=f"{path}[{i}]"))
        return findings

    def redact(self, data: Any) -> Any:
        """Returns a copy of data with credential values replaced by [REDACTED]."""
        if isinstance(data, str):
            result = data
            for name, pattern in CREDENTIAL_PATTERNS:
                result = pattern.sub("[REDACTED]", result)
            return result
        elif isinstance(data, dict):
            return {k: self.redact(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.redact(item) for item in data]
        return data
```

## Comparison

| Approach | Prevents Log Exposure | Scoped Permissions | Expiry | Encryption |
|---|---|---|---|---|
| ScopedCredentialToken | Yes (ref only) | Yes | Yes (TTL + max_uses) | No |
| AgentCredentialBroker | Yes (resolve at use) | Yes | Yes | No |
| CredentialEnvelopeFactory | Yes (encrypted) | No | Yes (TTL) | Yes (Fernet) |
| LeastPrivilegeTokenMinter | Via broker | Yes (policy-enforced) | Via broker | No |
| CredentialAuditLogger | Yes (ref only) | N/A | N/A | N/A |
| CredentialLeakDetector | Yes (scan + redact) | N/A | N/A | N/A |

**Best for production**: Register all credentials in `AgentCredentialBroker` at startup — never pass raw values. Use `LeastPrivilegeTokenMinter` to mint tokens with exactly the scopes each tool needs. Pass only the `token_id` (not the token object) in tool call arguments. Have the tool call `broker.resolve()` with its own `agent_id` to get the credential at use time. Run `CredentialLeakDetector.scan()` on every outbound tool argument and every inbound tool result before logging. Log all credential operations to `CredentialAuditLogger` for compliance.
