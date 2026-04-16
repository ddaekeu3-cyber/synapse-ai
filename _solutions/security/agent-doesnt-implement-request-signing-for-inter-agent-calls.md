---
layout: solution
title: "Agent Doesn't Implement Request Signing for Inter-Agent Calls"
description: "How to cryptographically sign requests between agents so receiving agents can verify the sender's identity, detect tampering, and reject replayed or forged calls."
tags: [security, authentication, inter-agent, hmac, jwt, signing, integrity]
difficulty: advanced
solution_count: 6
---

## Problem

In multi-agent systems, orchestrator agents call subagents over HTTP or message queues. These calls carry tool arguments, partial results, and instructions — but the receiving agent has no way to verify the message actually came from the expected sender, hasn't been tampered with in transit, or isn't being replayed from a previous session.

```python
# Bad: subagent blindly trusts any call that arrives
@app.post("/execute-tool")
async def execute_tool(request: ToolRequest):
    return await tools[request.tool_name](**request.args)
    # Any caller — including an attacker — can POST here
```

---

## Solution 1 — HMAC-SHA256 Request Signing

Sign the request body and timestamp with a shared HMAC secret. Verifying agents check the signature and reject requests older than a configurable window.

```python
import hashlib
import hmac
import json
import time
import os
from dataclasses import dataclass
from typing import Any

SHARED_SECRET = os.environ["INTER_AGENT_SECRET"]  # 32+ bytes, shared out-of-band

@dataclass
class SignedRequest:
    payload: dict
    agent_id: str
    timestamp: float
    nonce: str
    signature: str

def sign_request(payload: dict, agent_id: str) -> SignedRequest:
    nonce = os.urandom(16).hex()
    timestamp = time.time()
    message = json.dumps({
        "payload": payload,
        "agent_id": agent_id,
        "timestamp": timestamp,
        "nonce": nonce,
    }, sort_keys=True)
    signature = hmac.new(
        SHARED_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return SignedRequest(payload, agent_id, timestamp, nonce, signature)

class RequestVerifier:
    def __init__(self, max_age_seconds: float = 30.0):
        self._max_age = max_age_seconds
        self._seen_nonces: set[str] = set()

    def verify(self, req: SignedRequest) -> None:
        # 1. Check freshness
        age = time.time() - req.timestamp
        if age > self._max_age or age < -5:  # -5 allows minor clock skew
            raise PermissionError(
                f"Request timestamp out of window: age={age:.1f}s, max={self._max_age}s"
            )
        # 2. Check replay
        if req.nonce in self._seen_nonces:
            raise PermissionError(f"Replay detected: nonce already used")
        # 3. Verify signature
        message = json.dumps({
            "payload": req.payload,
            "agent_id": req.agent_id,
            "timestamp": req.timestamp,
            "nonce": req.nonce,
        }, sort_keys=True)
        expected = hmac.new(
            SHARED_SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, req.signature):
            raise PermissionError("Signature mismatch — request may be forged or tampered")
        # 4. Record nonce (evict old ones periodically in production)
        self._seen_nonces.add(req.nonce)

verifier = RequestVerifier(max_age_seconds=30)

# Orchestrator side
signed = sign_request(
    payload={"tool": "search", "query": "agent security"},
    agent_id="orchestrator-001",
)

# Subagent side
verifier.verify(signed)
result = await execute_tool(**signed.payload)
```

---

## Solution 2 — Per-Agent Asymmetric Key Pairs (Ed25519)

Each agent generates its own Ed25519 key pair at startup. Agents exchange public keys via a trusted key registry. Signatures cannot be forged without the private key.

```python
import json
import time
import os
from dataclasses import dataclass
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)
from cryptography.exceptions import InvalidSignature
import base64

@dataclass
class AgentKeyPair:
    agent_id: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @classmethod
    def generate(cls, agent_id: str) -> "AgentKeyPair":
        private = Ed25519PrivateKey.generate()
        return cls(agent_id, private, private.public_key())

    def public_key_bytes(self) -> bytes:
        return self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)

class AgentPublicKeyRegistry:
    """Trusted store of agent public keys. In production: backed by Vault or AWS KMS."""

    def __init__(self):
        self._keys: dict[str, Ed25519PublicKey] = {}

    def register(self, agent_id: str, public_key_bytes: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        # Reconstruct from raw bytes
        key = Ed25519PrivateKey.generate().public_key().__class__  # type hint only
        # Use raw public key import
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self._keys[agent_id] = public_key_bytes  # store raw for simplicity

    def get(self, agent_id: str) -> bytes | None:
        return self._keys.get(agent_id)

key_registry = AgentPublicKeyRegistry()

def make_signed_envelope(keypair: AgentKeyPair, payload: dict) -> dict:
    ts = time.time()
    nonce = os.urandom(16).hex()
    body = json.dumps({
        "payload": payload,
        "agent_id": keypair.agent_id,
        "ts": ts,
        "nonce": nonce,
    }, sort_keys=True).encode()
    sig = base64.b64encode(keypair.sign(body)).decode()
    return {
        "body": body.decode(),
        "agent_id": keypair.agent_id,
        "signature": sig,
        "ts": ts,
        "nonce": nonce,
    }

def verify_envelope(envelope: dict, max_age: float = 30.0) -> dict:
    age = time.time() - envelope["ts"]
    if abs(age) > max_age:
        raise PermissionError(f"Envelope expired: {age:.1f}s")

    pub_bytes = key_registry.get(envelope["agent_id"])
    if not pub_bytes:
        raise PermissionError(f"Unknown agent: {envelope['agent_id']}")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    # Reconstruct public key and verify
    sig = base64.b64decode(envelope["signature"])
    body = envelope["body"].encode()
    try:
        # Real Ed25519 verification
        import nacl.signing
        verify_key = nacl.signing.VerifyKey(pub_bytes)
        verify_key.verify(body, sig)
    except Exception as e:
        raise PermissionError(f"Invalid signature: {e}")

    return json.loads(envelope["body"])["payload"]

# Usage
keypair = AgentKeyPair.generate("orchestrator-001")
key_registry.register("orchestrator-001", keypair.public_key_bytes())

envelope = make_signed_envelope(keypair, {"tool": "search", "q": "test"})
payload = verify_envelope(envelope)
```

---

## Solution 3 — Short-Lived JWT Tokens for Agent Authorization

Issue JWT tokens to agents at startup. Each inter-agent call includes a JWT proving the caller's identity and authorized scopes. Tokens expire quickly (minutes) to limit blast radius.

```python
import time
import json
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

JWT_SECRET = os.environ["JWT_SIGNING_SECRET"]

def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def base64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)

def issue_agent_jwt(agent_id: str, scopes: list[str],
                     ttl_seconds: float = 300.0) -> str:
    """Issue a short-lived JWT for an agent."""
    header = base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "iss": "agent-auth-service",
        "sub": agent_id,
        "scopes": scopes,
        "iat": int(time.time()),
        "exp": int(time.time() + ttl_seconds),
        "jti": os.urandom(16).hex(),
    }
    payload = base64url_encode(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{base64url_encode(sig)}"

def verify_agent_jwt(token: str, required_scope: str | None = None) -> dict:
    """Verify JWT and return claims. Raises on invalid/expired token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise PermissionError("Malformed JWT")

    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected_sig = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    actual_sig = base64url_decode(sig_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise PermissionError("JWT signature invalid — possible forgery")

    claims = json.loads(base64url_decode(payload_b64))

    if claims.get("exp", 0) < time.time():
        raise PermissionError(f"JWT expired: exp={claims['exp']}, now={int(time.time())}")

    if required_scope and required_scope not in claims.get("scopes", []):
        raise PermissionError(
            f"Insufficient scope: token has {claims['scopes']}, needs '{required_scope}'"
        )

    return claims

# Agent startup: request a token
orchestrator_token = issue_agent_jwt(
    agent_id="orchestrator-001",
    scopes=["tool:search", "tool:calculate", "subagent:invoke"],
    ttl_seconds=300,
)

# Subagent verifies incoming call
def handle_tool_call(authorization: str, tool: str, args: dict) -> Any:
    if not authorization.startswith("Bearer "):
        raise PermissionError("Missing Bearer token")
    token = authorization[7:]
    claims = verify_agent_jwt(token, required_scope=f"tool:{tool}")
    print(f"Authorized call from {claims['sub']} with scopes {claims['scopes']}")
    return execute_tool(tool, **args)

handle_tool_call(f"Bearer {orchestrator_token}", "search", {"query": "test"})
```

---

## Solution 4 — Request Signing Middleware for FastAPI Subagents

Drop-in FastAPI middleware that enforces HMAC request signing on all inter-agent endpoints without modifying individual route handlers.

```python
import hashlib
import hmac
import json
import time
import os
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SHARED_SECRET = os.environ.get("INTER_AGENT_SECRET", "dev-secret")
SIGNED_PATH_PREFIX = "/internal/"  # only sign /internal/* endpoints

class HMACSigningMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret: str, max_age: float = 30.0,
                 enforce_on_paths: str = "/internal/"):
        super().__init__(app)
        self._secret = secret
        self._max_age = max_age
        self._prefix = enforce_on_paths
        self._seen_nonces: set[str] = set()

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(self._prefix):
            return await call_next(request)  # only enforce on internal paths

        # Read and buffer body (needed for signature verification)
        body = await request.body()

        try:
            self._verify(request, body)
        except PermissionError as e:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "detail": str(e)},
            )

        return await call_next(request)

    def _verify(self, request: Request, body: bytes) -> None:
        sig_header = request.headers.get("X-Agent-Signature")
        ts_header = request.headers.get("X-Agent-Timestamp")
        nonce_header = request.headers.get("X-Agent-Nonce")
        agent_id = request.headers.get("X-Agent-ID")

        if not all([sig_header, ts_header, nonce_header, agent_id]):
            raise PermissionError("Missing signing headers")

        ts = float(ts_header)
        age = time.time() - ts
        if abs(age) > self._max_age:
            raise PermissionError(f"Request age {age:.1f}s exceeds limit {self._max_age}s")

        if nonce_header in self._seen_nonces:
            raise PermissionError("Replay detected")
        self._seen_nonces.add(nonce_header)

        # Compute expected signature
        message = f"{agent_id}:{ts_header}:{nonce_header}:{request.method}:{request.url.path}:{body.decode()}"
        expected = hmac.new(self._secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise PermissionError("Signature mismatch")

def make_signing_headers(agent_id: str, method: str, path: str,
                          body: str, secret: str) -> dict[str, str]:
    ts = str(time.time())
    nonce = os.urandom(16).hex()
    message = f"{agent_id}:{ts}:{nonce}:{method}:{path}:{body}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Agent-ID": agent_id,
        "X-Agent-Timestamp": ts,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": sig,
        "Content-Type": "application/json",
    }

# Setup
app = FastAPI()
app.add_middleware(HMACSigningMiddleware,
                   secret=SHARED_SECRET,
                   enforce_on_paths="/internal/")

@app.post("/internal/execute-tool")
async def execute_tool_endpoint(request: Request):
    body = await request.json()
    return {"result": f"executed {body.get('tool')}"}

# Calling agent
import httpx
async def call_subagent(tool: str, args: dict) -> dict:
    body_str = json.dumps({"tool": tool, "args": args})
    headers = make_signing_headers(
        agent_id="orchestrator-001",
        method="POST",
        path="/internal/execute-tool",
        body=body_str,
        secret=SHARED_SECRET,
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://subagent:8000/internal/execute-tool",
            content=body_str,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## Solution 5 — Mutual TLS (mTLS) for Agent-to-Agent Authentication

Configure agents to present client certificates when calling each other. The receiving agent verifies the caller's certificate against a shared CA, providing strong mutual authentication.

```python
import ssl
import httpx
from pathlib import Path

# Directory structure:
# certs/
#   ca.crt          — shared CA certificate
#   orchestrator.crt — orchestrator's certificate
#   orchestrator.key — orchestrator's private key
#   subagent.crt     — subagent's certificate (to verify incoming calls)
#   subagent.key

CERT_DIR = Path("certs")

def make_mtls_client(agent_name: str) -> httpx.AsyncClient:
    """Create an httpx client that presents the agent's certificate."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True

    # Load CA to verify server certificates
    ctx.load_verify_locations(cafile=str(CERT_DIR / "ca.crt"))

    # Load this agent's certificate and private key (client cert)
    ctx.load_cert_chain(
        certfile=str(CERT_DIR / f"{agent_name}.crt"),
        keyfile=str(CERT_DIR / f"{agent_name}.key"),
    )

    return httpx.AsyncClient(verify=ctx)

def make_mtls_server_context(agent_name: str) -> ssl.SSLContext:
    """SSL context for a server that requires client certificates."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED  # require client cert
    ctx.load_verify_locations(cafile=str(CERT_DIR / "ca.crt"))
    ctx.load_cert_chain(
        certfile=str(CERT_DIR / f"{agent_name}.crt"),
        keyfile=str(CERT_DIR / f"{agent_name}.key"),
    )
    return ctx

# FastAPI: extract client agent identity from mTLS certificate
from fastapi import Request

def get_caller_agent_id(request: Request) -> str:
    """Extract the calling agent's identity from the mTLS client certificate."""
    # When using uvicorn behind nginx/envoy, client cert info is forwarded
    # via headers set by the proxy after verifying the mTLS handshake
    client_cert_subject = request.headers.get("X-Forwarded-Client-Cert-Subject")
    if not client_cert_subject:
        raise PermissionError("No client certificate presented")
    # Parse CN from subject: CN=orchestrator-001,O=AgentCorp,...
    for part in client_cert_subject.split(","):
        if part.strip().startswith("CN="):
            return part.strip()[3:]
    raise PermissionError("Cannot extract agent ID from certificate subject")

# Usage
async def call_subagent_mtls(url: str) -> dict:
    async with make_mtls_client("orchestrator") as client:
        resp = await client.post(url, json={"tool": "search", "query": "agents"})
        resp.raise_for_status()
        return resp.json()
```

---

## Solution 6 — Message Queue Signing with Dead-Letter Queue for Invalid Messages

Sign messages before enqueuing; consuming agents verify before processing. Invalid messages go to a dead-letter queue for investigation rather than failing silently.

```python
import asyncio
import json
import hmac
import hashlib
import time
import os
from dataclasses import dataclass, asdict
from typing import Any

QUEUE_SECRET = os.environ.get("QUEUE_SIGNING_SECRET", "dev-queue-secret")

@dataclass
class SignedMessage:
    payload: dict
    sender_id: str
    timestamp: float
    nonce: str
    signature: str
    message_id: str

def sign_message(payload: dict, sender_id: str) -> SignedMessage:
    ts = time.time()
    nonce = os.urandom(16).hex()
    msg_id = os.urandom(8).hex()
    canonical = json.dumps({
        "payload": payload,
        "sender_id": sender_id,
        "ts": ts,
        "nonce": nonce,
        "msg_id": msg_id,
    }, sort_keys=True)
    sig = hmac.new(QUEUE_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return SignedMessage(payload, sender_id, ts, nonce, sig, msg_id)

class SecureMessageQueue:
    """In-memory queue with HMAC signing. Replace internals with Redis/SQS in production."""

    def __init__(self, max_age: float = 60.0):
        self._queue: asyncio.Queue[SignedMessage] = asyncio.Queue()
        self._dlq: asyncio.Queue[dict] = asyncio.Queue()  # dead-letter queue
        self._seen: set[str] = set()
        self._max_age = max_age

    async def send(self, payload: dict, sender_id: str) -> str:
        msg = sign_message(payload, sender_id)
        await self._queue.put(msg)
        return msg.message_id

    def _verify(self, msg: SignedMessage) -> None:
        age = time.time() - msg.timestamp
        if abs(age) > self._max_age:
            raise PermissionError(f"Message too old: {age:.1f}s")
        if msg.nonce in self._seen:
            raise PermissionError("Replay detected")
        canonical = json.dumps({
            "payload": msg.payload,
            "sender_id": msg.sender_id,
            "ts": msg.timestamp,
            "nonce": msg.nonce,
            "msg_id": msg.message_id,
        }, sort_keys=True)
        expected = hmac.new(QUEUE_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, msg.signature):
            raise PermissionError("Signature invalid — message may be tampered")
        self._seen.add(msg.nonce)

    async def receive_verified(self) -> SignedMessage:
        while True:
            msg = await self._queue.get()
            try:
                self._verify(msg)
                return msg
            except PermissionError as e:
                await self._dlq.put({"msg": asdict(msg), "error": str(e)})
                print(f"[DLQ] Invalid message from {msg.sender_id}: {e}")
                # Continue to next message

    async def drain_dlq(self) -> list[dict]:
        items = []
        while not self._dlq.empty():
            items.append(self._dlq.get_nowait())
        return items

# Usage
queue = SecureMessageQueue()

async def producer():
    for i in range(5):
        msg_id = await queue.send(
            payload={"task": f"summarize-{i}", "priority": "high"},
            sender_id="orchestrator-001",
        )
        print(f"Sent: {msg_id}")
    # Send a tampered message
    fake = SignedMessage(
        payload={"task": "inject_malicious_command"},
        sender_id="attacker",
        timestamp=time.time(),
        nonce=os.urandom(16).hex(),
        signature="invalid_signature",
        message_id="fake-001",
    )
    await queue._queue.put(fake)

async def consumer():
    for _ in range(6):  # 5 valid + 1 invalid
        try:
            msg = await asyncio.wait_for(queue.receive_verified(), timeout=2.0)
            print(f"Processing: {msg.payload['task']} from {msg.sender_id}")
        except asyncio.TimeoutError:
            break
    dlq = await queue.drain_dlq()
    print(f"DLQ entries: {len(dlq)}")

asyncio.run(asyncio.gather(producer(), consumer()))
```

---

## Comparison

| Approach | Key Distribution | Prevents Replay | Mutual Auth | Overhead | Best For |
|---|---|---|---|---|---|
| HMAC-SHA256 | Shared secret | **Yes** (nonce+timestamp) | No | **Lowest** | Small trusted agent clusters |
| Ed25519 asymmetric | Key registry | **Yes** | Partial | Low | Medium-scale agent meshes |
| Short-lived JWT | Token service | Partial (exp claim) | No | Low | Scope-based authorization |
| FastAPI middleware | Shared secret | **Yes** | No | Low | HTTP-based subagents |
| Mutual TLS (mTLS) | PKI / CA | **Yes** | **Yes** | Medium | Production zero-trust networks |
| Queue message signing | Shared secret | **Yes** | No | Low | Async message-passing agents |
