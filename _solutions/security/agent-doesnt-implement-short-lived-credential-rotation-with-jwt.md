---
title: "Agent Doesn't Implement Short-Lived Credential Rotation with JWT"
description: "How to issue short-lived JWT access tokens with automatic refresh rotation, sliding expiry, refresh token families, and revocation — preventing credential theft from causing long-lived unauthorized access in AI agent systems."
date: 2025-01-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-short-lived-credential-rotation-with-jwt
tags:
  - security
  - jwt
  - credential-rotation
  - token-refresh
  - authentication
  - refresh-tokens
  - revocation
symptoms:
  - "Long-lived API tokens never expire — stolen credentials remain valid indefinitely"
  - "No refresh token rotation — replaying a captured refresh token still works"
  - "JWT access tokens valid for hours or days instead of minutes"
  - "No way to revoke a specific token without rotating the signing key"
  - "Agent re-authenticates from scratch on every request (no token caching)"
  - "Token introspection adds latency on every request because no local validation"
---

## Why This Happens

Many agent systems use long-lived API keys or JWTs for simplicity. If a token is stolen (via log injection, memory dump, or intercepted request), the attacker has persistent access for as long as the token is valid. The industry standard is short-lived access tokens (5–15 minutes) paired with longer-lived refresh tokens (days or weeks). Access tokens are validated locally (fast, no DB roundtrip); refresh tokens are validated against a server-side store where they can be revoked.

Without rotation, a captured refresh token can be used to generate new access tokens indefinitely. Refresh token families solve this: when a refresh token is used, it is immediately invalidated and replaced with a new one. If an attacker and the legitimate client both try to use the same refresh token, the system detects the conflict and revokes the entire family.

---

## Solution 1: JWT Access Token Issuer

Issue short-lived, signed JWT access tokens with proper claims and local validation.

```python
import jwt
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class TokenClaims:
    sub: str           # Subject (user/agent ID)
    iss: str           # Issuer
    aud: str           # Audience
    iat: float         # Issued at
    exp: float         # Expiry
    jti: str           # JWT ID (unique per token)
    scopes: list[str]  # Authorized scopes
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sub": self.sub,
            "iss": self.iss,
            "aud": self.aud,
            "iat": int(self.iat),
            "exp": int(self.exp),
            "jti": self.jti,
            "scopes": self.scopes,
            **self.extra,
        }


class JWTTokenIssuer:
    """
    Issues short-lived JWT access tokens signed with RS256 or HS256.
    Tokens expire quickly to limit blast radius of credential theft.
    """

    def __init__(
        self,
        secret_or_private_key: str,
        algorithm: str = "HS256",
        issuer: str = "agent-auth",
        audience: str = "agent-api",
        access_ttl_seconds: int = 900,     # 15 minutes
    ):
        self._key = secret_or_private_key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = access_ttl_seconds

    def issue_access_token(
        self,
        subject: str,
        scopes: list[str],
        extra_claims: dict | None = None,
    ) -> tuple[str, TokenClaims]:
        now = time.time()
        claims = TokenClaims(
            sub=subject,
            iss=self._issuer,
            aud=self._audience,
            iat=now,
            exp=now + self._access_ttl,
            jti=str(uuid.uuid4()),
            scopes=scopes,
            extra=extra_claims or {},
        )
        token = jwt.encode(claims.to_dict(), self._key, algorithm=self._algorithm)
        return token, claims

    def validate(
        self,
        token: str,
        required_scopes: list[str] | None = None,
        public_key: str | None = None,
    ) -> TokenClaims:
        """
        Validate token signature, expiry, issuer, and audience locally.
        No database roundtrip needed for access token validation.
        """
        key = public_key or self._key
        payload = jwt.decode(
            token,
            key,
            algorithms=[self._algorithm],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )

        claims = TokenClaims(
            sub=payload["sub"],
            iss=payload["iss"],
            aud=payload["aud"],
            iat=payload["iat"],
            exp=payload["exp"],
            jti=payload["jti"],
            scopes=payload.get("scopes", []),
        )

        if required_scopes:
            missing = set(required_scopes) - set(claims.scopes)
            if missing:
                raise jwt.exceptions.InvalidClaimsError(
                    f"Missing required scopes: {missing}"
                )
        return claims

    def time_until_expiry(self, claims: TokenClaims) -> float:
        return max(0.0, claims.exp - time.time())
```

---

## Solution 2: Refresh Token Manager with Rotation

Issue refresh tokens with server-side storage. On use, immediately rotate to a new token. Detect reuse attacks via token families.

```python
import secrets
import time
from dataclasses import dataclass, field

@dataclass
class RefreshToken:
    token_hash: str          # SHA-256 of the raw token (stored, not the raw token)
    subject: str
    family_id: str           # All tokens in a rotation chain share a family_id
    issued_at: float
    expires_at: float
    used: bool = False
    revoked: bool = False
    parent_hash: Optional[str] = None

class RefreshTokenStore:
    """In-memory refresh token store (replace with persistent DB in production)."""

    def __init__(self):
        self._tokens: dict[str, RefreshToken] = {}  # hash -> RefreshToken
        self._families: dict[str, list[str]] = {}   # family_id -> [token_hashes]

    def save(self, rt: RefreshToken) -> None:
        self._tokens[rt.token_hash] = rt
        self._families.setdefault(rt.family_id, []).append(rt.token_hash)

    def get(self, token_hash: str) -> Optional[RefreshToken]:
        return self._tokens.get(token_hash)

    def revoke_family(self, family_id: str) -> int:
        """Revoke all tokens in a family (used when reuse is detected)."""
        count = 0
        for h in self._families.get(family_id, []):
            if h in self._tokens:
                self._tokens[h].revoked = True
                count += 1
        return count

    def mark_used(self, token_hash: str) -> None:
        if token_hash in self._tokens:
            self._tokens[token_hash].used = True


class RefreshTokenManager:
    """
    Manages refresh token issuance, rotation, and revocation.
    Implements the token family pattern to detect theft via reuse.
    """

    def __init__(
        self,
        store: RefreshTokenStore,
        ttl_seconds: int = 604_800,  # 7 days
    ):
        self._store = store
        self._ttl = ttl_seconds

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    def issue(
        self,
        subject: str,
        family_id: Optional[str] = None,
        parent_hash: Optional[str] = None,
    ) -> str:
        """Issue a new refresh token. Returns the raw token (show once, never stored)."""
        raw = secrets.token_urlsafe(48)
        token_hash = self._hash_token(raw)
        now = time.time()
        rt = RefreshToken(
            token_hash=token_hash,
            subject=subject,
            family_id=family_id or str(uuid.uuid4()),
            issued_at=now,
            expires_at=now + self._ttl,
            parent_hash=parent_hash,
        )
        self._store.save(rt)
        return raw

    def rotate(self, raw_token: str) -> tuple[str, str]:
        """
        Exchange a refresh token for a new access token + rotated refresh token.
        Returns (new_refresh_raw, subject).
        Raises on invalid, expired, used, or revoked tokens.
        """
        token_hash = self._hash_token(raw_token)
        rt = self._store.get(token_hash)

        if rt is None:
            raise ValueError("Unknown refresh token")
        if rt.revoked:
            raise ValueError("Refresh token has been revoked")
        if time.time() > rt.expires_at:
            raise ValueError("Refresh token has expired")

        if rt.used:
            # Reuse detected — entire family may be compromised
            revoked_count = self._store.revoke_family(rt.family_id)
            raise ValueError(
                f"Refresh token reuse detected! Revoked {revoked_count} tokens "
                f"in family {rt.family_id}. Possible token theft."
            )

        # Mark current token as used
        self._store.mark_used(token_hash)

        # Issue new rotation
        new_raw = self.issue(
            subject=rt.subject,
            family_id=rt.family_id,
            parent_hash=token_hash,
        )
        return new_raw, rt.subject

    def revoke(self, raw_token: str) -> None:
        """Explicitly revoke a refresh token (e.g., on logout)."""
        token_hash = self._hash_token(raw_token)
        rt = self._store.get(token_hash)
        if rt:
            self._store.revoke_family(rt.family_id)
```

---

## Solution 3: Agent Token Cache with Auto-Refresh

Agents cache their access token and automatically refresh before expiry, avoiding authentication on every request.

```python
import asyncio
import time
from typing import Optional, Callable, Awaitable

class AgentTokenCache:
    """
    Caches an access token and automatically refreshes it before expiry.
    Thread-safe: concurrent requests share one refresh operation.
    """

    def __init__(
        self,
        refresh_fn: Callable[[str], Awaitable[tuple[str, float]]],
        initial_refresh_token: str,
        refresh_buffer_seconds: float = 60.0,
    ):
        self._refresh_fn = refresh_fn  # (refresh_token) -> (access_token, expiry_ts)
        self._refresh_token = initial_refresh_token
        self._buffer = refresh_buffer_seconds
        self._access_token: Optional[str] = None
        self._expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._refreshing: Optional[asyncio.Future] = None

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expiry - self._buffer:
            return self._access_token

        async with self._lock:
            # Double-check after acquiring lock
            if self._access_token and time.time() < self._expiry - self._buffer:
                return self._access_token

            # Refresh
            new_access, new_expiry = await self._refresh_fn(self._refresh_token)
            self._access_token = new_access
            self._expiry = new_expiry
            return self._access_token

    async def invalidate(self) -> None:
        """Force refresh on next use (e.g., after 401 response)."""
        async with self._lock:
            self._access_token = None
            self._expiry = 0.0

    @property
    def seconds_until_expiry(self) -> float:
        return max(0.0, self._expiry - time.time())


class TokenAwareHTTPClient:
    """HTTP client that automatically attaches and refreshes JWT bearer tokens."""

    def __init__(self, token_cache: AgentTokenCache):
        self._cache = token_cache

    async def request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> Any:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            token = await self._cache.get_access_token()
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {token}"

            async with session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status == 401:
                    # Token might have been revoked — force refresh and retry once
                    await self._cache.invalidate()
                    token = await self._cache.get_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    async with session.request(method, url, headers=headers, **kwargs) as retry:
                        retry.raise_for_status()
                        return await retry.json()

                resp.raise_for_status()
                return await resp.json()
```

---

## Solution 4: Access Token Revocation via Blocklist

Short-lived tokens don't need revocation for most use cases, but when you need immediate revocation (compromise, logout), maintain a compact blocklist indexed by JTI.

```python
import asyncio
import time
from typing import Optional

class TokenBlocklist:
    """
    Compact blocklist of revoked JWT IDs (jti claims).
    Only stores revocations until the token would have expired anyway.
    """

    def __init__(self):
        self._blocked: dict[str, float] = {}  # jti -> expiry_ts
        self._cleanup_task: Optional[asyncio.Task] = None

    def revoke(self, jti: str, expiry_ts: float) -> None:
        """Add a JTI to the blocklist until its natural expiry."""
        self._blocked[jti] = expiry_ts

    def is_revoked(self, jti: str) -> bool:
        expiry = self._blocked.get(jti)
        if expiry is None:
            return False
        if time.time() > expiry:
            # Expired — no longer needed in blocklist
            del self._blocked[jti]
            return False
        return True

    def revoke_by_subject(self, subject: str, jti_list: list[str], expiry_ts: float) -> None:
        """Revoke all active tokens for a subject (e.g., on password change)."""
        for jti in jti_list:
            self.revoke(jti, expiry_ts)

    def start_cleanup(self, interval: float = 300.0) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval))

    async def _cleanup_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            expired = [jti for jti, exp in self._blocked.items() if now > exp]
            for jti in expired:
                del self._blocked[jti]

    def __len__(self) -> int:
        return len(self._blocked)


class BlocklistAwareTokenValidator:
    """Validates JWTs with blocklist check for revoked tokens."""

    def __init__(self, issuer: JWTTokenIssuer, blocklist: TokenBlocklist):
        self.issuer = issuer
        self.blocklist = blocklist

    def validate(self, token: str, required_scopes: list[str] | None = None) -> TokenClaims:
        claims = self.issuer.validate(token, required_scopes=required_scopes)
        if self.blocklist.is_revoked(claims.jti):
            raise jwt.exceptions.InvalidClaimsError(f"Token {claims.jti} has been revoked")
        return claims
```

---

## Solution 5: Service-to-Service JWT with Mutual Authentication

For agent-to-agent communication, issue short-lived service tokens that carry the caller's identity and authorized scopes.

```python
import time
import uuid

class ServiceTokenExchange:
    """
    Issues short-lived service tokens for agent-to-agent API calls.
    Tokens are scoped to specific service pairs and expire in minutes.
    """

    SERVICE_TOKEN_TTL = 300  # 5 minutes

    def __init__(self, issuer: JWTTokenIssuer, service_id: str):
        self._issuer = issuer
        self._service_id = service_id

    def issue_service_token(
        self,
        target_service: str,
        scopes: list[str],
        context: dict | None = None,
    ) -> str:
        """Issue a token for calling `target_service`."""
        token, _ = self._issuer.issue_access_token(
            subject=self._service_id,
            scopes=scopes,
            extra_claims={
                "target": target_service,
                "service_context": context or {},
                "token_type": "service",
            },
        )
        return token

    def validate_incoming(
        self,
        token: str,
        expected_caller: Optional[str] = None,
        required_scopes: Optional[list[str]] = None,
    ) -> TokenClaims:
        """Validate an incoming service token."""
        claims = self._issuer.validate(token, required_scopes=required_scopes)

        if claims.extra.get("target") != self._service_id:
            raise ValueError(
                f"Token issued for '{claims.extra.get('target')}', not '{self._service_id}'"
            )
        if expected_caller and claims.sub != expected_caller:
            raise ValueError(
                f"Expected caller '{expected_caller}', got '{claims.sub}'"
            )
        return claims
```

---

## Solution 6: Full Auth Flow Orchestrator

Combines access token issuance, refresh rotation, blocklist, and token cache into a single auth service.

```python
class AgentAuthService:
    """
    Complete authentication service for AI agents.
    Manages the full lifecycle: issue, refresh, validate, revoke.
    """

    def __init__(
        self,
        secret_key: str,
        access_ttl: int = 900,
        refresh_ttl: int = 604_800,
    ):
        self._issuer = JWTTokenIssuer(
            secret_or_private_key=secret_key,
            access_ttl_seconds=access_ttl,
        )
        self._refresh_store = RefreshTokenStore()
        self._refresh_manager = RefreshTokenManager(self._refresh_store, ttl_seconds=refresh_ttl)
        self._blocklist = TokenBlocklist()
        self._validator = BlocklistAwareTokenValidator(self._issuer, self._blocklist)

    def authenticate(self, subject: str, scopes: list[str]) -> dict:
        """Issue a new access + refresh token pair."""
        access_token, claims = self._issuer.issue_access_token(subject, scopes)
        refresh_token = self._refresh_manager.issue(subject)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": self._issuer._access_ttl,
            "token_type": "Bearer",
        }

    def refresh(self, refresh_token: str, scopes: list[str]) -> dict:
        """Exchange a refresh token for a new access + refresh pair."""
        new_refresh, subject = self._refresh_manager.rotate(refresh_token)
        access_token, claims = self._issuer.issue_access_token(subject, scopes)
        return {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "expires_in": self._issuer._access_ttl,
            "token_type": "Bearer",
        }

    def validate(self, access_token: str, required_scopes: list[str] | None = None) -> TokenClaims:
        """Validate an access token — fast local check, no DB needed."""
        return self._validator.validate(access_token, required_scopes=required_scopes)

    def revoke_access_token(self, access_token: str) -> None:
        """Immediately revoke a specific access token via blocklist."""
        try:
            claims = self._issuer.validate(access_token)
            self._blocklist.revoke(claims.jti, claims.exp)
        except Exception:
            pass  # Already invalid

    def logout(self, refresh_token: str) -> None:
        """Revoke refresh token family (all tokens for this session)."""
        self._refresh_manager.revoke(refresh_token)
```

---

## Comparison

| Solution | Token Lifetime | Revocation | Theft Detection | DB Roundtrip | Best For |
|---|---|---|---|---|---|
| JWT Access Token Issuer | Short (15 min) | Expiry only | No | No (local) | All access tokens |
| Refresh Token with Rotation | Long (7 days) | Immediate | Family reuse detection | Yes | Long sessions with refresh |
| Agent Token Cache | N/A (client) | Via refresh | No | No | Client-side auto-refresh |
| JTI Blocklist | Until expiry | Immediate | No | In-memory | Emergency revocation |
| Service-to-Service Tokens | Very short (5 min) | Expiry only | No | No | Agent-to-agent calls |
| Full Auth Service | Short + Long | Full | Yes | Minimal | Production auth system |

**Issue access tokens with 15-minute TTL** — this is the single most impactful change. **Always rotate refresh tokens** on use and implement family-based reuse detection to catch stolen tokens. **Use the token cache** in every agent so authentication doesn't add latency to each request. **Add the JTI blocklist** only if you need immediate revocation beyond natural expiry — for most cases, short TTL plus refresh rotation is sufficient. **Issue service-to-service tokens** for inter-agent calls rather than sharing long-lived API keys.
