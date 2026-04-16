---
title: "Agent Doesn't Implement OAuth2 PKCE for Browser Agents"
description: "Browser-based AI agents use the implicit grant or authorization code flow without PKCE; authorization codes can be intercepted and exchanged by a malicious app on the same device."
category: security
difficulty: advanced
tags: [oauth2, pkce, authorization, browser, security, tokens, authentication, code-verifier]
---

# Agent Doesn't Implement OAuth2 PKCE for Browser Agents

## Problem

Browser-based and mobile agents that use OAuth2 without PKCE (Proof Key for Code Exchange, RFC 7636) are vulnerable to authorization code interception attacks. An attacker who intercepts the authorization code — via a malicious app registered to the same redirect URI, a browser history leak, or a referrer header — can exchange it for access tokens. PKCE binds each authorization request to a cryptographic secret that only the legitimate client possesses, making intercepted codes useless.

## Solution 1: Full PKCE Authorization Code Flow

Generate a code verifier + challenge pair, include the challenge in the authorization request, and verify with the raw verifier at token exchange.

```python
import asyncio
import base64
import hashlib
import secrets
import urllib.parse
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# ── PKCE primitives ──────────────────────────────────────────────────────────

def generate_code_verifier(length: int = 64) -> str:
    """
    RFC 7636 §4.1: code_verifier is a high-entropy cryptographic random string.
    Length: 43–128 characters. Allowed chars: unreserved URI chars.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))

def generate_code_challenge(verifier: str, method: str = "S256") -> str:
    """
    RFC 7636 §4.2: code_challenge = BASE64URL(SHA256(ASCII(code_verifier)))
    Always use S256; plain is only for legacy clients that cannot do SHA-256.
    """
    if method != "S256":
        raise ValueError("Only S256 is acceptable; plain is insecure")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

# ── Authorization request builder ────────────────────────────────────────────

def build_authorization_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the authorization URL with PKCE parameters."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"

# ── Token exchange ────────────────────────────────────────────────────────────

async def exchange_code_for_token(
    token_endpoint: str,
    client_id: str,
    redirect_uri: str,
    authorization_code: str,
    code_verifier: str,    # raw verifier — proves we initiated the flow
) -> dict:
    """
    Exchange authorization code + code_verifier for access/refresh tokens.
    The authorization server recomputes challenge from verifier and compares
    to what was sent at authorization time.
    """
    import httpx
    async with httpx.AsyncClient() as http:
        resp = await http.post(token_endpoint, data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": authorization_code,
            "code_verifier": code_verifier,  # PKCE: no client_secret needed
        })
        resp.raise_for_status()
        return resp.json()

# ── Complete PKCE flow coordinator ───────────────────────────────────────────

class PKCEFlowCoordinator:
    """
    Manages the full PKCE flow for a browser-based agent.
    The code_verifier is stored in memory; never persisted to disk or localStorage.
    """

    def __init__(self, client_id: str, redirect_uri: str,
                 authorization_endpoint: str, token_endpoint: str):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self._pending: dict[str, str] = {}  # state -> code_verifier

    def start_flow(self, scope: str = "openid profile") -> str:
        """
        Step 1: Generate verifier + challenge + state.
        Returns the authorization URL to redirect the user to.
        """
        code_verifier = generate_code_verifier()
        code_challenge = generate_code_challenge(code_verifier)
        state = secrets.token_urlsafe(32)

        # Store verifier keyed by state (survives browser redirect)
        self._pending[state] = code_verifier

        return build_authorization_url(
            self.authorization_endpoint,
            self.client_id,
            self.redirect_uri,
            scope,
            code_challenge,
            state,
        )

    async def complete_flow(self, code: str, state: str) -> dict:
        """
        Step 2: Called when the browser returns to redirect_uri with ?code=&state=.
        Validates state (CSRF) and exchanges code + verifier for tokens.
        """
        code_verifier = self._pending.pop(state, None)
        if code_verifier is None:
            raise ValueError("Invalid or expired state — possible CSRF attack")

        tokens = await exchange_code_for_token(
            self.token_endpoint,
            self.client_id,
            self.redirect_uri,
            code,
            code_verifier,
        )
        return tokens

# Usage
async def demo():
    coordinator = PKCEFlowCoordinator(
        client_id="my-agent-client",
        redirect_uri="https://agent.example.com/callback",
        authorization_endpoint="https://auth.example.com/oauth2/authorize",
        token_endpoint="https://auth.example.com/oauth2/token",
    )

    # Step 1: redirect user
    auth_url = coordinator.start_flow(scope="openid profile agent:read")
    print(f"Redirect user to: {auth_url}")

    # Step 2: after redirect back (in real app, parse ?code=&state= from URL)
    # tokens = await coordinator.complete_flow(code="...", state="...")
    # access_token = tokens["access_token"]
```

**When to use**: Any browser-based or mobile agent using OAuth2. PKCE is mandatory per RFC 9700 for public clients; it replaces the implicit flow entirely.

---

## Solution 2: State + Nonce Binding — Prevent CSRF and Replay

Bind the authorization request to both a `state` (CSRF) and a `nonce` (replay prevention), and validate both on return.

```python
import asyncio
import hashlib
import json
import secrets
import time
import base64

class SecureOAuthState:
    """
    Stores per-flow state with expiry. Validates on callback to prevent:
    - CSRF (state mismatch)
    - Replay (nonce already used)
    - Expired flows (TTL)
    """

    def __init__(self, ttl_seconds: int = 300):
        self._flows: dict[str, dict] = {}
        self._ttl = ttl_seconds
        self._used_nonces: set[str] = set()

    def create_flow(self, scope: str, redirect_uri: str) -> dict:
        """Create a new flow and return the parameters to embed in the auth URL."""
        code_verifier = generate_code_verifier()
        code_challenge = generate_code_challenge(code_verifier)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)

        self._flows[state] = {
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
            "nonce": nonce,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }

        return {
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

    def validate_callback(self, state: str, code: str) -> dict:
        """
        Validate callback parameters. Returns flow data including code_verifier.
        Raises on any security violation.
        """
        flow = self._flows.pop(state, None)
        if flow is None:
            raise ValueError("Unknown state — CSRF attack or double-submission")

        if time.time() - flow["created_at"] > self._ttl:
            raise ValueError(f"Authorization flow expired after {self._ttl}s")

        nonce = flow["nonce"]
        if nonce in self._used_nonces:
            raise ValueError("Nonce already used — replay attack detected")
        self._used_nonces.add(nonce)

        return flow

    def _prune_expired(self):
        cutoff = time.time() - self._ttl
        expired = [s for s, f in self._flows.items() if f["created_at"] < cutoff]
        for s in expired:
            del self._flows[s]

def generate_code_verifier(length: int = 64) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))

def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

# Usage
store = SecureOAuthState(ttl_seconds=300)

# At authorization initiation:
flow_params = store.create_flow(scope="openid", redirect_uri="https://agent.example.com/callback")
# Embed flow_params["state"], flow_params["nonce"], flow_params["code_challenge"] in auth URL

# At callback:
# flow = store.validate_callback(state=request.query["state"], code=request.query["code"])
# Then exchange flow["code_verifier"] + code for tokens
```

**When to use**: Add to Solution 1 for defense in depth. State validates CSRF; nonce validates ID token binding; TTL prevents abandoned flows being exploited later.

---

## Solution 3: Token Storage — Secure In-Memory Store, Never localStorage

Store access tokens in memory (JavaScript closure or server-side session), never in `localStorage` or `sessionStorage` where XSS can exfiltrate them.

```python
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TokenRecord:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    scope: str
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: float = 30.0) -> bool:
        return time.time() >= (self.expires_at - buffer_seconds)

class SecureTokenStore:
    """
    Server-side token store keyed by opaque session cookie.
    Access tokens never leave the server; browser only holds an opaque session ID.
    This prevents XSS-based token exfiltration.
    """

    def __init__(self, max_idle_seconds: int = 3600):
        self._tokens: dict[str, TokenRecord] = {}
        self._last_accessed: dict[str, float] = {}
        self._max_idle = max_idle_seconds

    def store(self, token_response: dict) -> str:
        """Store token response; return opaque session key for browser cookie."""
        session_key = secrets.token_urlsafe(32)
        expires_in = token_response.get("expires_in", 3600)
        self._tokens[session_key] = TokenRecord(
            access_token=token_response["access_token"],
            refresh_token=token_response.get("refresh_token"),
            expires_at=time.time() + expires_in,
            scope=token_response.get("scope", ""),
        )
        self._last_accessed[session_key] = time.time()
        return session_key  # set as HttpOnly, Secure, SameSite=Strict cookie

    def get_access_token(self, session_key: str) -> Optional[str]:
        """Return access token for a session; None if expired or missing."""
        self._prune_idle()
        record = self._tokens.get(session_key)
        if record is None:
            return None
        if record.is_expired():
            return None
        self._last_accessed[session_key] = time.time()
        return record.access_token

    async def refresh_if_needed(
        self,
        session_key: str,
        token_endpoint: str,
        client_id: str,
    ) -> Optional[str]:
        """Refresh access token using refresh_token if it's expiring soon."""
        record = self._tokens.get(session_key)
        if record is None:
            return None
        if not record.is_expired():
            return record.access_token
        if record.refresh_token is None:
            return None

        import httpx
        async with httpx.AsyncClient() as http:
            resp = await http.post(token_endpoint, data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": record.refresh_token,
            })
            if resp.status_code != 200:
                del self._tokens[session_key]
                return None
            new_tokens = resp.json()

        new_key = self.store(new_tokens)
        # Migrate session: keep same session_key pointing to new tokens
        self._tokens[session_key] = self._tokens.pop(new_key)
        return self._tokens[session_key].access_token

    def revoke(self, session_key: str) -> None:
        self._tokens.pop(session_key, None)
        self._last_accessed.pop(session_key, None)

    def _prune_idle(self):
        cutoff = time.time() - self._max_idle
        idle = [k for k, t in self._last_accessed.items() if t < cutoff]
        for k in idle:
            self._tokens.pop(k, None)
            self._last_accessed.pop(k, None)

token_store = SecureTokenStore()

async def call_agent_with_oauth(session_key: str, user_message: str) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    access_token = token_store.get_access_token(session_key)
    if access_token is None:
        return {"error": "unauthenticated", "action": "redirect_to_login"}

    # Use access_token as bearer for downstream API calls
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        extra_headers={"X-User-Token": access_token},
        messages=[{"role": "user", "content": user_message}],
    )
    return {"response": resp.content[0].text}
```

**When to use**: All browser-based agents. Storing tokens server-side with an opaque cookie eliminates the XSS exfiltration surface entirely.

---

## Solution 4: Token Introspection — Validate Tokens on Every Agent Request

Don't trust access tokens blindly; introspect them at the authorization server on each agent request to detect revoked or tampered tokens.

```python
import asyncio
import time
from functools import lru_cache
from typing import Optional
import httpx

INTROSPECTION_CACHE: dict[str, tuple[dict, float]] = {}
INTROSPECTION_TTL = 30  # re-introspect every 30 seconds

async def introspect_token(
    token: str,
    introspection_endpoint: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    RFC 7662 token introspection.
    Returns token metadata or {"active": false} if invalid/revoked.
    """
    # Cache to avoid hitting the auth server on every request
    cache_key = f"{token[:16]}..."  # don't log full token
    if cache_key in INTROSPECTION_CACHE:
        cached, expires_at = INTROSPECTION_CACHE[cache_key]
        if time.time() < expires_at:
            return cached

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            introspection_endpoint,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        result = resp.json()

    INTROSPECTION_CACHE[cache_key] = (result, time.time() + INTROSPECTION_TTL)
    return result

async def require_valid_token(
    token: str,
    required_scope: str,
    introspection_endpoint: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Validate token and required scope. Raises on invalid/insufficient token.
    Returns token claims on success.
    """
    claims = await introspect_token(token, introspection_endpoint, client_id, client_secret)

    if not claims.get("active", False):
        raise PermissionError("Token is inactive, expired, or revoked")

    token_scopes = set(claims.get("scope", "").split())
    required = set(required_scope.split())
    if not required.issubset(token_scopes):
        missing = required - token_scopes
        raise PermissionError(f"Token missing required scopes: {missing}")

    exp = claims.get("exp", 0)
    if exp and time.time() > exp:
        raise PermissionError("Token is expired")

    return claims

async def agent_endpoint(bearer_token: str, user_message: str) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    try:
        claims = await require_valid_token(
            token=bearer_token,
            required_scope="agent:execute",
            introspection_endpoint="https://auth.example.com/oauth2/introspect",
            client_id="agent-backend",
            client_secret="server-secret",
        )
    except PermissionError as exc:
        return {"error": "unauthorized", "detail": str(exc)}

    user_id = claims.get("sub", "unknown")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are assisting user {user_id}.",
        messages=[{"role": "user", "content": user_message}],
    )
    return {"response": resp.content[0].text, "user": user_id}
```

**When to use**: Agents where token revocation must take effect immediately (e.g., admin revokes access mid-session). Introspection detects revoked tokens that haven't expired yet.

---

## Solution 5: DPoP-Bound Tokens — Prevent Token Theft via Bearer Replay

Bind access tokens to a proof-of-possession key (DPoP, RFC 9449). A stolen bearer token cannot be used without the corresponding private key.

```python
import asyncio
import base64
import hashlib
import json
import secrets
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

def generate_dpop_keypair() -> tuple[Ed25519PrivateKey, dict]:
    """Generate an ephemeral Ed25519 keypair for DPoP."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Export public key as JWK
    raw_pub = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode(),
    }
    return private_key, jwk

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def create_dpop_proof(
    private_key: Ed25519PrivateKey,
    jwk: dict,
    http_method: str,
    http_uri: str,
    access_token: str | None = None,
) -> str:
    """
    Create a DPoP proof JWT (RFC 9449 §4.2).
    Each proof is bound to one HTTP method + URI + moment in time.
    """
    header = {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": jwk}
    payload = {
        "jti": secrets.token_urlsafe(16),
        "htm": http_method.upper(),
        "htu": http_uri,
        "iat": int(time.time()),
    }
    if access_token:
        # Bind proof to specific access token (ath claim)
        ath = base64.urlsafe_b64encode(
            hashlib.sha256(access_token.encode()).digest()
        ).rstrip(b"=").decode()
        payload["ath"] = ath

    header_b64 = _b64url(json.dumps(header).encode())
    payload_b64 = _b64url(json.dumps(payload).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = private_key.sign(signing_input)

    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"

async def dpop_api_call(
    access_token: str,
    private_key: Ed25519PrivateKey,
    jwk: dict,
    url: str,
    method: str = "POST",
    body: dict | None = None,
) -> dict:
    """Make an API call with DPoP-bound token."""
    import httpx
    dpop_proof = create_dpop_proof(private_key, jwk, method, url, access_token)

    async with httpx.AsyncClient() as http:
        resp = await http.request(
            method,
            url,
            headers={
                "Authorization": f"DPoP {access_token}",
                "DPoP": dpop_proof,
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

# Usage: generate keypair once per session
private_key, jwk = generate_dpop_keypair()
# Include jwk in token request so server binds token to this key
# Provide dpop_proof on each API call
```

**When to use**: High-value agent sessions (financial, medical). DPoP means stolen bearer tokens are useless without the matching private key.

---

## Solution 6: Silent Refresh — Renew Tokens Before Expiry Without User Interaction

Proactively refresh access tokens before they expire so the agent never has an authentication gap mid-conversation.

```python
import asyncio
import time
from typing import Callable, Awaitable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class SilentRefreshManager:
    """
    Manages proactive token refresh for long-running agent sessions.
    Refreshes when remaining lifetime drops below a threshold.
    """

    def __init__(
        self,
        refresh_fn: Callable[[], Awaitable[dict]],
        refresh_threshold_seconds: float = 60.0,
    ):
        self._refresh_fn = refresh_fn
        self._threshold = refresh_threshold_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    async def initialize(self, token_response: dict) -> None:
        self._update_from_response(token_response)
        self._schedule_refresh()

    def _update_from_response(self, token_response: dict) -> None:
        self._access_token = token_response["access_token"]
        expires_in = token_response.get("expires_in", 3600)
        self._expires_at = time.time() + expires_in

    def _schedule_refresh(self) -> None:
        remaining = self._expires_at - time.time()
        refresh_in = max(0, remaining - self._threshold)

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

        self._refresh_task = asyncio.create_task(self._refresh_loop(refresh_in))

    async def _refresh_loop(self, initial_delay: float) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            async with self._lock:
                try:
                    new_tokens = await self._refresh_fn()
                    self._update_from_response(new_tokens)
                except Exception as exc:
                    print(f"Silent refresh failed: {exc}")
                    # Retry in 5 seconds
                    await asyncio.sleep(5)
                    continue

            # Sleep until next refresh window
            remaining = self._expires_at - time.time()
            await asyncio.sleep(max(0, remaining - self._threshold))

    async def get_token(self) -> str | None:
        async with self._lock:
            if time.time() < self._expires_at:
                return self._access_token
            return None  # expired and refresh hasn't completed

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

async def agent_session(refresh_manager: SilentRefreshManager, messages: list[str]) -> list[dict]:
    results = []
    for msg in messages:
        token = await refresh_manager.get_token()
        if token is None:
            results.append({"error": "no_valid_token", "message": msg})
            continue

        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": msg}],
        )
        results.append({"response": resp.content[0].text})
    return results

# Usage
async def demo():
    async def mock_refresh() -> dict:
        return {"access_token": secrets.token_urlsafe(32), "expires_in": 3600}

    import secrets
    manager = SilentRefreshManager(refresh_fn=mock_refresh, refresh_threshold_seconds=60)
    initial_tokens = await mock_refresh()
    await manager.initialize(initial_tokens)

    results = await agent_session(manager, ["Hello", "What is 2+2?"])
    await manager.stop()
    return results
```

**When to use**: Long-running agent sessions (hours) where access tokens expire mid-conversation. Silent refresh ensures the agent never interrupts a user conversation to re-authenticate.

---

## Comparison

| Solution | Intercepted Code | XSS Exfiltration | Token Theft | Revocation | Long Sessions | Best For |
|---|---|---|---|---|---|---|
| PKCE flow | Prevented | No | No | No | No | All OAuth2 browser agents (mandatory) |
| State + nonce binding | Prevented | No | No | No | No | CSRF + replay resistance |
| Server-side token store | Prevented | Prevented | No | No | No | XSS-safe token storage |
| Token introspection | No | No | No | Immediate | No | Real-time revocation enforcement |
| DPoP binding | No | No | Prevented | No | No | High-value sessions |
| Silent refresh | No | No | No | No | Enabled | Long-running agent conversations |

**Rule of thumb**: Always use PKCE (Solution 1) — it is mandatory per RFC 9700 for public clients. Add server-side token storage (Solution 3) to eliminate XSS risk. Add silent refresh (Solution 6) for agents that run for hours.
