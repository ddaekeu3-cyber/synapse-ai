---
title: "Agent Doesn't Implement Token Binding for API Sessions"
description: "AI agents issue bearer tokens that can be replayed from any origin; without token binding the credential is stolen the moment it leaks from memory, logs, or a MITM."
category: security
difficulty: advanced
tags: [token-binding, dpop, mtls, session-security, jwt, authentication, cryptography]
---

# Agent Doesn't Implement Token Binding for API Sessions

## Problem

Bearer tokens are secrets that grant access to whoever possesses them. If a token leaks — via logs, a compromised proxy, a SSRF response, or memory dump — an attacker can replay it from anywhere. Token binding ties a credential to a specific cryptographic key or TLS channel so a stolen token is worthless without the corresponding private key.

## Solution 1: DPoP (Demonstrating Proof of Possession) JWT Binding

RFC 9449 DPoP binds an access token to a client-held asymmetric key pair. Each request includes a signed proof-of-possession JWT alongside the access token.

```python
import time
import uuid
import json
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import jwt  # PyJWT

class DPoPClient:
    """Client that attaches DPoP proof to every request."""

    def __init__(self):
        self._private_key = Ed25519PrivateKey.generate()
        pub = self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": base64.urlsafe_b64encode(pub).rstrip(b"=").decode(),
        }

    def _make_proof(self, method: str, url: str, access_token: str | None = None) -> str:
        now = int(time.time())
        claims = {
            "jti": str(uuid.uuid4()),
            "htm": method.upper(),
            "htu": url,
            "iat": now,
            "exp": now + 60,  # proof valid for 60 seconds only
        }
        if access_token:
            # Bind the proof to the specific access token hash
            import hashlib
            ath = base64.urlsafe_b64encode(
                hashlib.sha256(access_token.encode()).digest()
            ).rstrip(b"=").decode()
            claims["ath"] = ath

        header = {"alg": "EdDSA", "typ": "dpop+jwt", "jwk": self._jwk}
        return jwt.encode(claims, self._private_key, algorithm="EdDSA", headers=header)

    async def request(self, method: str, url: str, access_token: str, **kwargs):
        import aiohttp
        proof = self._make_proof(method, url, access_token)
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"DPoP {access_token}"
        headers["DPoP"] = proof
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                return await resp.json()

# Server-side DPoP proof verification
def verify_dpop_proof(dpop_header: str, method: str, url: str, access_token: str) -> bool:
    try:
        unverified = jwt.decode(dpop_header, options={"verify_signature": False})
        jwk = jwt.get_unverified_header(dpop_header).get("jwk")
        if not jwk:
            return False

        # Reconstruct public key from JWK
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_bytes = base64.urlsafe_b64decode(jwk["x"] + "==")
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

        claims = jwt.decode(dpop_header, pub_key, algorithms=["EdDSA"])

        # Validate method, URL, and token hash
        import hashlib
        expected_ath = base64.urlsafe_b64encode(
            hashlib.sha256(access_token.encode()).digest()
        ).rstrip(b"=").decode()

        return (
            claims["htm"] == method.upper()
            and claims["htu"] == url
            and claims.get("ath") == expected_ath
        )
    except Exception:
        return False
```

**When to use**: OAuth 2.0 flows where you control both client and authorization server. RFC 9449 is widely supported.

---

## Solution 2: Mutual TLS (mTLS) Channel Binding

Bind tokens to the client's TLS certificate. The server verifies both the certificate and that the token was issued to that certificate's public key thumbprint.

```python
import ssl
import hashlib
import base64
import aiohttp
from pathlib import Path

def cert_thumbprint(cert_pem: bytes) -> str:
    """SHA-256 thumbprint of DER-encoded certificate (RFC 8705 cnf.x5t#S256)."""
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.DER)
    digest = hashlib.sha256(der).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

class MTLSAgentClient:
    def __init__(self, cert_path: str, key_path: str, ca_path: str):
        self._ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self._ssl_ctx.load_cert_chain(cert_path, key_path)
        self._ssl_ctx.load_verify_locations(ca_path)
        self._ssl_ctx.verify_mode = ssl.CERT_REQUIRED

        cert_pem = Path(cert_path).read_bytes()
        self._thumbprint = cert_thumbprint(cert_pem)

    async def get_bound_token(self, auth_server_url: str) -> str:
        """Request a certificate-bound access token."""
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                auth_server_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": "agent-service",
                    # Server reads client cert from TLS and embeds thumbprint in token
                },
            ) as resp:
                data = await resp.json()
                return data["access_token"]

    async def api_call(self, url: str, token: str):
        """Call API over mTLS; server verifies cert thumbprint matches token's cnf claim."""
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                return await resp.json()

# Server-side: verify token's cnf.x5t#S256 matches presented client cert
def verify_mtls_binding(token_claims: dict, client_cert_der: bytes) -> bool:
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(client_cert_der).digest()
    ).rstrip(b"=").decode()
    cnf = token_claims.get("cnf", {})
    return cnf.get("x5t#S256") == expected
```

**When to use**: Internal service mesh communication, high-security agent-to-agent calls, zero-trust architectures.

---

## Solution 3: HMAC Request Signing (Token + Canonical Request Binding)

Sign each request with an HMAC derived from the session token and request details. Token replay without the signing key fails because the signature won't match.

```python
import hmac
import hashlib
import time
import json
import aiohttp
from urllib.parse import urlparse

class HMACBoundClient:
    """Binds token usage to specific requests via HMAC signature."""

    def __init__(self, access_token: str, signing_secret: str):
        self._token = access_token
        self._secret = signing_secret.encode()

    def _canonical_request(self, method: str, url: str, body: bytes, timestamp: int) -> bytes:
        parsed = urlparse(url)
        body_hash = hashlib.sha256(body).hexdigest()
        return f"{method.upper()}\n{parsed.path}\n{parsed.query}\n{timestamp}\n{body_hash}".encode()

    def _sign(self, canonical: bytes) -> str:
        return hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()

    async def request(self, method: str, url: str, json_body: dict | None = None):
        body = json.dumps(json_body).encode() if json_body else b""
        ts = int(time.time())
        canonical = self._canonical_request(method, url, body, ts)
        sig = self._sign(canonical)

        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Timestamp": str(ts),
            "X-Signature": sig,
            "X-Signature-Algorithm": "HMAC-SHA256",
        }

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, data=body) as resp:
                return await resp.json()

# Server-side verification
def verify_hmac_bound_request(
    method: str, url: str, body: bytes,
    timestamp_header: str, sig_header: str,
    token_claims: dict, signing_secret: str,
) -> bool:
    # Reject stale requests (replay window = 5 minutes)
    ts = int(timestamp_header)
    if abs(time.time() - ts) > 300:
        return False

    secret = signing_secret.encode()
    parsed = urlparse(url)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method.upper()}\n{parsed.path}\n{parsed.query}\n{ts}\n{body_hash}".encode()
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)
```

**When to use**: When you can't do mTLS or DPoP but still want replay resistance beyond just nonce-based replay prevention.

---

## Solution 4: Session Fingerprint Binding (IP + User-Agent + TLS Session ID)

Bind sessions to a fingerprint of connection properties. Suspicious mismatch triggers re-authentication.

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

@dataclass
class SessionFingerprint:
    ip_subnet: str           # /24 subnet, not exact IP (NAT-friendly)
    user_agent_hash: str
    tls_session_hash: str    # hash of TLS session resumption ID if available

    def digest(self, secret: str) -> str:
        data = json.dumps({
            "ip_subnet": self.ip_subnet,
            "ua": self.user_agent_hash,
            "tls": self.tls_session_hash,
        }, sort_keys=True).encode()
        return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()[:32]

class FingerprintBoundSessionStore:
    def __init__(self, secret: str, tolerance: int = 1):
        self._secret = secret
        self._tolerance = tolerance  # how many fields can differ before forcing reauth
        self._sessions: dict[str, tuple[SessionFingerprint, float]] = {}

    def create_session(self, session_id: str, fingerprint: SessionFingerprint) -> str:
        self._sessions[session_id] = (fingerprint, time.monotonic())
        return fingerprint.digest(self._secret)

    def validate(self, session_id: str, current: SessionFingerprint) -> tuple[bool, str]:
        if session_id not in self._sessions:
            return False, "unknown_session"

        stored_fp, created_at = self._sessions[session_id]

        mismatches = sum([
            stored_fp.ip_subnet != current.ip_subnet,
            stored_fp.user_agent_hash != current.user_agent_hash,
            stored_fp.tls_session_hash != current.tls_session_hash,
        ])

        if mismatches > self._tolerance:
            del self._sessions[session_id]  # invalidate suspected hijack
            return False, f"fingerprint_mismatch:{mismatches}_fields"

        return True, "ok"

# FastAPI middleware integration
from fastapi import Request, HTTPException
import ipaddress

def extract_fingerprint(request: Request) -> SessionFingerprint:
    ip = request.client.host
    # Use /24 subnet to tolerate NAT address rotation
    subnet = str(ipaddress.ip_network(f"{ip}/24", strict=False))
    ua_hash = hashlib.sha256(
        request.headers.get("user-agent", "").encode()
    ).hexdigest()[:16]
    tls_id = request.headers.get("x-tls-session-id", "none")
    tls_hash = hashlib.sha256(tls_id.encode()).hexdigest()[:16]
    return SessionFingerprint(subnet, ua_hash, tls_hash)
```

**When to use**: Web-facing agents where you want to detect session hijacking without full crypto binding.

---

## Solution 5: Token Rotation with Usage-Count Binding

Bind tokens to a usage counter. Each request increments and returns a new token, making replayed old tokens invalid.

```python
import asyncio
import secrets
import time
import hashlib
import json
import redis.asyncio as aioredis

class RotatingBoundTokenStore:
    """Single-use or bounded-use tokens stored in Redis."""

    def __init__(self, redis_url: str = "redis://localhost:6379", max_uses: int = 1):
        self._redis = aioredis.from_url(redis_url)
        self._max_uses = max_uses

    def _token_key(self, token: str) -> str:
        h = hashlib.sha256(token.encode()).hexdigest()
        return f"bound_token:{h}"

    async def issue(self, subject: str, ttl_seconds: int = 3600) -> str:
        token = secrets.token_urlsafe(32)
        data = json.dumps({"sub": subject, "uses": 0, "max_uses": self._max_uses, "iat": int(time.time())})
        await self._redis.setex(self._token_key(token), ttl_seconds, data)
        return token

    async def consume(self, token: str) -> tuple[bool, dict | None]:
        """Atomically verify and increment usage count."""
        key = self._token_key(token)

        async def _txn(pipe):
            raw = await pipe.get(key)
            if not raw:
                return None
            data = json.loads(raw)
            if data["uses"] >= data["max_uses"]:
                return None
            pipe.multi()
            data["uses"] += 1
            ttl = await self._redis.ttl(key)
            pipe.setex(key, max(ttl, 1), json.dumps(data))
            return data

        result = await self._redis.transaction(_txn, key)
        if result is None:
            return False, None
        return True, result

    async def rotate(self, old_token: str, ttl_seconds: int = 3600) -> str | None:
        """Invalidate old token and issue new one bound to same subject."""
        ok, data = await self.consume(old_token)
        if not ok:
            return None
        new_token = await self.issue(data["sub"], ttl_seconds)
        await self._redis.delete(self._token_key(old_token))
        return new_token

# Usage
store = RotatingBoundTokenStore(max_uses=100)

async def handle_api_request(token: str, payload: dict):
    ok, data = await store.consume(token)
    if not ok:
        raise PermissionError("Token expired, exhausted, or replayed")
    # Process request with data["sub"] as authenticated subject
    return {"result": "processed", "for": data["sub"]}
```

**When to use**: High-value one-shot tokens (e.g., file download links, webhook delivery tokens, tool execution grants).

---

## Solution 6: Audience + Issuer Pinning in JWT Validation

Strictly validate `aud`, `iss`, `sub`, and binding claims so tokens issued for one agent service cannot be replayed against another.

```python
import jwt
import time
import json
from dataclasses import dataclass
from typing import Any

@dataclass
class TokenValidationPolicy:
    required_issuer: str
    required_audience: str
    required_subject_prefix: str
    max_age_seconds: int = 3600
    required_claims: list[str] = None

    def __post_init__(self):
        if self.required_claims is None:
            self.required_claims = ["iat", "exp", "jti", "sub", "iss", "aud"]

class StrictTokenValidator:
    def __init__(self, policy: TokenValidationPolicy, public_key_pem: str):
        self._policy = policy
        self._public_key = public_key_pem
        self._seen_jtis: set[str] = set()  # jti replay prevention (in-memory; use Redis in prod)

    def validate(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256", "ES256", "EdDSA"],
                audience=self._policy.required_audience,
                issuer=self._policy.required_issuer,
                options={
                    "require": self._policy.required_claims,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWTError as e:
            raise ValueError(f"Token validation failed: {e}")

        # Subject prefix check (e.g., "agent:prod:" prefix required)
        if not claims["sub"].startswith(self._policy.required_subject_prefix):
            raise ValueError(f"Invalid subject: {claims['sub']}")

        # Max age (belt-and-suspenders beyond exp)
        age = time.time() - claims["iat"]
        if age > self._policy.max_age_seconds:
            raise ValueError(f"Token too old: {age:.0f}s")

        # JTI replay prevention
        jti = claims["jti"]
        if jti in self._seen_jtis:
            raise ValueError(f"Replayed JTI: {jti}")
        self._seen_jtis.add(jti)
        # TODO: store in Redis with TTL matching token expiry for multi-instance deployments

        return claims

# Usage
policy = TokenValidationPolicy(
    required_issuer="https://auth.internal/",
    required_audience="agent-service-prod",
    required_subject_prefix="agent:prod:",
    max_age_seconds=900,
)
validator = StrictTokenValidator(policy, public_key_pem=open("public.pem").read())

def authenticate_request(auth_header: str) -> dict:
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing Bearer token")
    token = auth_header[7:]
    return validator.validate(token)  # raises on any violation
```

**When to use**: All JWT-accepting endpoints. Strict audience pinning prevents cross-service token confusion attacks.

---

## Comparison

| Solution | Crypto Strength | Client Complexity | Replay Resistance | Standard | Best For |
|---|---|---|---|---|---|
| DPoP JWT | High (asymmetric) | Medium | Yes (per-request) | RFC 9449 | OAuth 2.0 APIs |
| mTLS | Very High | High (PKI) | Yes (channel-bound) | RFC 8705 | Internal service mesh |
| HMAC request signing | Medium | Low | Yes (per-request) | Custom | APIs without OAuth |
| Fingerprint binding | Low-Medium | None | Partial | Custom | Web sessions, CSRF complement |
| Rotating tokens | Medium | Low | Yes (use-count) | Custom | One-shot grants |
| JWT audience pinning | Medium | None | Yes (jti) | RFC 7519 | All JWT consumers |

**Rule of thumb**: Use DPoP for public-facing APIs, mTLS for internal service mesh, strict audience pinning everywhere. Always include JTI replay prevention regardless of binding method.
