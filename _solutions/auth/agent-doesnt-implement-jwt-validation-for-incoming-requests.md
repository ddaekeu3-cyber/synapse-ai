---
layout: solution
title: "Agent Doesn't Validate JWT Tokens on Incoming Requests"
category: auth
description: "Agents that skip JWT signature verification, expiry checks, or audience validation allow forged tokens to impersonate any user."
tags: [auth, jwt, security, fastapi, pyjwt, validation]
---

# Agent Doesn't Validate JWT Tokens on Incoming Requests

Many agents accept a bearer token in the `Authorization` header but only decode it without verifying the signature, expiry, issuer, or audience. A base64-decoded JWT with a fabricated payload passes right through. This gives attackers full access by crafting their own tokens.

## Why This Happens

`jwt.decode()` without `algorithms` or with `verify=False` is commonly copy-pasted. Developers decode the token to read the `sub` field but don't realize that decoding and verifying are separate operations.

---

## Option 1: PyJWT Signature + Claims Validation

Verify RS256/HS256 signature, expiry, issuer, and audience in one call.

```python
import jwt
from jwt import PyJWTError
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI()
security = HTTPBearer()

# For RS256: load your public key
# PUBLIC_KEY = open("public.pem").read()
# For HS256: use a shared secret
JWT_SECRET = "your-256-bit-secret"
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "https://auth.example.com"
JWT_AUDIENCE = "synapse-agent-api"


def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "require": ["sub", "exp", "iat", "iss", "aud"],
            },
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid issuer")
    except PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


@app.post("/agent/run")
async def run_agent(prompt: str, claims: dict = Depends(verify_jwt)):
    user_id = claims["sub"]
    return {"user_id": user_id, "result": f"Running agent for {user_id}"}
```

**Expected Token Savings:** No direct savings; prevents unauthorized API usage that could drain your token budget.

**Environment:** FastAPI + PyJWT; HS256 shared-secret or RS256 public-key setups.

---

## Option 2: RS256 with JWKS Endpoint (Production Standard)

Fetch public keys from a JWKS endpoint (like Auth0, Okta, Cognito) and cache them with rotation support.

```python
import time
import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI()
security = HTTPBearer()

JWKS_URL = "https://your-auth-provider.com/.well-known/jwks.json"
JWT_AUDIENCE = "your-api"
JWT_ISSUER = "https://your-auth-provider.com/"

# Simple in-memory JWKS cache
_jwks_cache: dict = {}
_jwks_cached_at: float = 0
JWKS_TTL = 3600  # 1 hour


def get_jwks() -> dict:
    global _jwks_cache, _jwks_cached_at
    if time.time() - _jwks_cached_at < JWKS_TTL:
        return _jwks_cache
    resp = httpx.get(JWKS_URL, timeout=5)
    resp.raise_for_status()
    _jwks_cache = {key["kid"]: key for key in resp.json()["keys"]}
    _jwks_cached_at = time.time()
    return _jwks_cache


def get_public_key(kid: str):
    """Fetch and cache JWKS, return RSA public key for given kid."""
    jwks = get_jwks()
    if kid not in jwks:
        # Refresh cache in case of key rotation
        global _jwks_cached_at
        _jwks_cached_at = 0
        jwks = get_jwks()
    if kid not in jwks:
        raise HTTPException(status_code=401, detail=f"Unknown key ID: {kid}")
    return RSAAlgorithm.from_jwk(jwks[kid])


def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Missing kid in token header")

        public_key = get_public_key(kid)
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"JWT validation failed: {e}")


@app.get("/me")
async def get_me(claims: dict = Depends(verify_jwt)):
    return {"sub": claims["sub"], "email": claims.get("email")}
```

**Expected Token Savings:** Industry-standard JWKS-based validation; handles key rotation automatically.

**Environment:** Auth0, Okta, Cognito, or any OIDC-compliant identity provider; production APIs.

---

## Option 3: Custom Claims Enforcement (Roles, Scopes)

After signature validation, enforce required custom claims like `role`, `scope`, or `tenant_id`.

```python
import jwt
from functools import wraps
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Callable

app = FastAPI()
security = HTTPBearer()

JWT_SECRET = "your-secret"
JWT_ALGORITHM = "HS256"


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "scope"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")


def require_scope(*required_scopes: str) -> Callable:
    """FastAPI dependency factory that enforces JWT scopes."""
    def dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict:
        claims = decode_token(credentials.credentials)
        token_scopes = set(claims.get("scope", "").split())
        missing = set(required_scopes) - token_scopes
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Missing required scopes: {missing}",
            )
        return claims
    return dependency


def require_role(*allowed_roles: str) -> Callable:
    """FastAPI dependency factory that enforces JWT roles."""
    def dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict:
        claims = decode_token(credentials.credentials)
        user_role = claims.get("role", "")
        if user_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user_role}' not authorized. Required: {allowed_roles}",
            )
        return claims
    return dependency


# Usage: scope enforcement
@app.post("/agent/execute")
async def execute_agent(
    prompt: str,
    claims: dict = Depends(require_scope("agent:execute")),
):
    return {"user": claims["sub"], "prompt": prompt}


# Usage: role enforcement
@app.delete("/agent/{agent_id}")
async def delete_agent(
    agent_id: str,
    claims: dict = Depends(require_role("admin", "superuser")),
):
    return {"deleted": agent_id, "by": claims["sub"]}
```

**Expected Token Savings:** Prevents unauthorized users from triggering expensive agent runs.

**Environment:** Multi-tenant APIs with RBAC or scope-based access control.

---

## Option 4: JWT Blacklist for Revocation

Maintain a revocation list so tokens can be invalidated before expiry (logout, key compromise).

```python
import time
import jwt
import sqlite3
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI()
security = HTTPBearer()

JWT_SECRET = "your-secret"
JWT_ALGORITHM = "HS256"

# SQLite-backed revocation list
conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.execute(
    "CREATE TABLE revoked_tokens (jti TEXT PRIMARY KEY, revoked_at REAL)"
)
conn.commit()


def revoke_token(jti: str):
    conn.execute(
        "INSERT OR REPLACE INTO revoked_tokens VALUES (?, ?)",
        (jti, time.time()),
    )
    conn.commit()


def is_revoked(jti: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)
    ).fetchone()
    return row is not None


def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(401, "Token missing jti claim")
    if is_revoked(jti):
        raise HTTPException(401, "Token has been revoked")

    return payload


@app.post("/logout")
async def logout(claims: dict = Depends(verify_jwt)):
    revoke_token(claims["jti"])
    return {"message": "Logged out", "jti": claims["jti"]}


@app.get("/protected")
async def protected(claims: dict = Depends(verify_jwt)):
    return {"user": claims["sub"]}
```

**Expected Token Savings:** Immediate revocation prevents compromised tokens from triggering agent runs.

**Environment:** Systems requiring logout or credential revocation; use Redis in production for shared state.

---

## Option 5: Middleware-Level JWT Guard

Apply JWT validation as ASGI middleware so all routes are protected without per-route dependencies.

```python
import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastapi import FastAPI

JWT_SECRET = "your-secret"
JWT_ALGORITHM = "HS256"
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}


class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "Missing bearer token"}, status_code=401)

        token = auth[7:]
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                options={"require": ["sub", "exp"]},
            )
            request.state.jwt_claims = payload
        except jwt.ExpiredSignatureError:
            return JSONResponse({"detail": "Token expired"}, status_code=401)
        except jwt.PyJWTError as e:
            return JSONResponse({"detail": f"Invalid token: {e}"}, status_code=401)

        return await call_next(request)


app = FastAPI()
app.add_middleware(JWTMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent/run")
async def run_agent(request: Request, prompt: str):
    claims = request.state.jwt_claims
    return {"user": claims["sub"], "prompt": prompt}
```

**Expected Token Savings:** Zero per-route code; all endpoints protected automatically; reduces boilerplate errors.

**Environment:** FastAPI/Starlette; uniform token enforcement across all routes.

---

## Option 6: JWT Validation Unit Tests

Test suite verifying that expired, tampered, wrong-algorithm, and missing-claims tokens are all rejected.

```python
import time
import pytest
import jwt
from fastapi.testclient import TestClient
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Minimal app for testing
SECRET = "test-secret-32-bytes-long-padding!"
ALGORITHM = "HS256"

app = FastAPI()
security = HTTPBearer()


def verify(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        return jwt.decode(
            credentials.credentials,
            SECRET,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "expired")
    except jwt.PyJWTError as e:
        raise HTTPException(401, str(e))


@app.get("/protected")
def protected(claims: dict = Depends(verify)):
    return {"sub": claims["sub"]}


client = TestClient(app, raise_server_exceptions=False)


def make_token(**kwargs) -> str:
    payload = {"sub": "user1", "exp": int(time.time()) + 3600, **kwargs}
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def test_valid_token():
    token = make_token()
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["sub"] == "user1"


def test_expired_token():
    token = make_token(exp=int(time.time()) - 10)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_wrong_algorithm():
    # Sign with RS256 but server expects HS256
    token = jwt.encode(
        {"sub": "user1", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",  # same secret but try passing as RS256 claim
    )
    # Tamper the header to claim RS256
    import base64, json
    parts = token.split(".")
    header = json.loads(base64.b64decode(parts[0] + "=="))
    header["alg"] = "RS256"
    bad_header = base64.b64encode(json.dumps(header).encode()).decode().rstrip("=")
    tampered = f"{bad_header}.{parts[1]}.{parts[2]}"
    resp = client.get("/protected", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


def test_tampered_payload():
    token = make_token()
    parts = token.split(".")
    # Flip a byte in the signature
    sig = parts[2]
    tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    tampered = f"{parts[0]}.{parts[1]}.{tampered_sig}"
    resp = client.get("/protected", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


def test_missing_sub_claim():
    token = jwt.encode({"exp": int(time.time()) + 3600}, SECRET, algorithm=ALGORITHM)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_no_token():
    resp = client.get("/protected")
    assert resp.status_code == 403  # FastAPI HTTPBearer returns 403 when missing


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Prevents auth bypass bugs that allow free agent usage; catches regressions on token validation logic.

**Environment:** CI pipeline; any FastAPI + PyJWT project.

---

## Comparison

| Option | Signature Algo | Expiry Check | Claims Enforcement | Revocation | Multi-Route |
|--------|---------------|--------------|-------------------|------------|-------------|
| 1. PyJWT full validation | HS256/RS256 | Yes | Required claims | No | Per-route |
| 2. JWKS endpoint | RS256 | Yes | Aud + Iss | No | Per-route |
| 3. Custom claims | HS256 | Yes | Roles + Scopes | No | Per-route |
| 4. Blacklist revocation | HS256 | Yes | JTI check | Yes | Per-route |
| 5. Middleware guard | HS256 | Yes | Basic | No | All routes |
| 6. Test suite | HS256 | Tested | Tested | Tested | N/A |
