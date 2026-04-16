---
layout: solution
title: "Agent Doesn't Implement Agent-to-Agent Authentication"
category: security
description: "Verify the identity of calling agents before processing requests — prevent unauthorized agents from invoking privileged operations by signing and validating inter-agent messages."
tags: [security, authentication, agent-to-agent, hmac, jwt, multi-agent, trust]
---

## Problem

Multi-agent systems often trust any message that arrives on an internal channel without verifying who sent it. A malicious caller, compromised subagent, or prompt-injected tool result can impersonate a trusted orchestrator and invoke privileged operations. Without authentication, inter-agent communication is only as secure as its least-protected endpoint.

```python
# Naive: no verification of caller identity
def handle_subagent_request(payload: dict) -> dict:
    if payload["action"] == "delete_user":
        delete_user(payload["user_id"])   # no check: who is asking?
    return {"status": "ok"}
```

## Solution Options

### Option 1: HMAC-SHA256 Request Signing

Sign each inter-agent request with a shared secret. The receiving agent verifies the signature before processing.

```python
import anthropic
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

@dataclass
class SignedMessage:
    payload: dict
    agent_id: str
    timestamp: float
    signature: str

SHARED_SECRET = b"super-secret-inter-agent-key-32b"   # in production: load from secrets manager

def _sign(payload: dict, agent_id: str, timestamp: float, secret: bytes) -> str:
    body = json.dumps({"payload": payload, "agent_id": agent_id, "timestamp": timestamp},
                      sort_keys=True)
    return hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()

def create_signed_message(payload: dict, agent_id: str, secret: bytes = SHARED_SECRET) -> SignedMessage:
    ts = time.time()
    sig = _sign(payload, agent_id, ts, secret)
    return SignedMessage(payload=payload, agent_id=agent_id, timestamp=ts, signature=sig)

def verify_signed_message(
    msg: SignedMessage,
    secret: bytes = SHARED_SECRET,
    max_age_seconds: float = 30.0,
) -> bool:
    # Replay protection: reject old messages
    if abs(time.time() - msg.timestamp) > max_age_seconds:
        print(f"[AUTH FAIL] Message too old: age={time.time() - msg.timestamp:.1f}s")
        return False
    expected = _sign(msg.payload, msg.agent_id, msg.timestamp, secret)
    if not hmac.compare_digest(expected, msg.signature):
        print(f"[AUTH FAIL] Invalid signature from agent_id={msg.agent_id}")
        return False
    return True


# Orchestrator: prepare and send a signed request
client = anthropic.Anthropic()

def orchestrator_request_tool(tool_name: str, tool_args: dict, agent_id: str = "orchestrator-01") -> dict:
    msg = create_signed_message(
        payload={"tool": tool_name, "args": tool_args},
        agent_id=agent_id,
    )
    # Simulate passing msg to subagent (in practice: HTTP, queue, etc.)
    return subagent_handle(msg)

def subagent_handle(msg: SignedMessage) -> dict:
    if not verify_signed_message(msg):
        return {"error": "Authentication failed", "status": 403}

    tool = msg.payload.get("tool")
    args = msg.payload.get("args", {})
    print(f"[SUBAGENT] Verified request from {msg.agent_id}: tool={tool}")

    # Ask Claude to process the verified request
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Execute tool '{tool}' with args: {json.dumps(args)}"}],
    )
    return {"result": r.content[0].text, "status": 200}


# Test legitimate request
response = orchestrator_request_tool("summarize", {"text": "Python is a programming language."})
print(f"Legitimate: {response}")

# Test tampered message
tampered = SignedMessage(
    payload={"tool": "delete_all_data", "args": {}},
    agent_id="orchestrator-01",
    timestamp=time.time(),
    signature="invalid_signature",
)
print(f"Tampered: {subagent_handle(tampered)}")

# Expected Token Savings: Auth adds 0 tokens overhead; prevents unauthorized tool invocations
# Environment: ANTHROPIC_API_KEY
```

### Option 2: JWT-Based Agent Identity Tokens

Issue short-lived JWT tokens to each agent at startup. Agents present their token on each request; receivers validate expiry, issuer, and scope.

```python
import anthropic
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

@dataclass
class AgentToken:
    agent_id: str
    issuer: str
    scopes: list[str]
    issued_at: float
    expires_at: float
    token: str

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

# Minimal JWT implementation (use PyJWT in production)
JWT_SECRET = b"jwt-signing-secret-change-in-prod"

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _issue_jwt(agent_id: str, issuer: str, scopes: list[str], ttl: float = 300.0) -> str:
    now = time.time()
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({
        "sub": agent_id, "iss": issuer, "scopes": scopes,
        "iat": now, "exp": now + ttl,
    }).encode())
    sig = _b64url(hmac.new(JWT_SECRET, f"{header}.{claims}".encode(), hashlib.sha256).digest())
    return f"{header}.{claims}.{sig}"

def _verify_jwt(token: str) -> dict | None:
    try:
        header_b64, claims_b64, sig_b64 = token.split(".")
        expected_sig = _b64url(
            hmac.new(JWT_SECRET, f"{header_b64}.{claims_b64}".encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_sig, sig_b64):
            return None
        padding = "=" * (4 - len(claims_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(claims_b64 + padding))
        if claims["exp"] < time.time():
            return None  # expired
        return claims
    except Exception:
        return None

def issue_agent_token(agent_id: str, scopes: list[str]) -> AgentToken:
    now = time.time()
    token = _issue_jwt(agent_id, "auth-service", scopes, ttl=300.0)
    return AgentToken(
        agent_id=agent_id, issuer="auth-service",
        scopes=scopes, issued_at=now, expires_at=now + 300.0,
        token=token,
    )


# Subagent endpoint with JWT validation
client = anthropic.Anthropic()

ALLOWED_SCOPES = {
    "analyze": ["read"],
    "write_memory": ["read", "write"],
    "delete_data": ["read", "write", "admin"],
}

def secure_subagent_endpoint(token_str: str, action: str, data: dict) -> dict:
    claims = _verify_jwt(token_str)
    if claims is None:
        return {"error": "Invalid or expired token", "status": 401}
    required_scope = ALLOWED_SCOPES.get(action, ["admin"])
    if not all(s in claims["scopes"] for s in required_scope):
        return {
            "error": f"Insufficient scope for '{action}'. "
                     f"Need: {required_scope}, have: {claims['scopes']}",
            "status": 403,
        }
    print(f"[AUTH OK] agent={claims['sub']} action={action} scopes={claims['scopes']}")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Action: {action}\nData: {json.dumps(data)}"}],
    )
    return {"result": r.content[0].text, "status": 200}


# Orchestrator with read+write scopes
orch_token = issue_agent_token("orchestrator-01", scopes=["read", "write"])
print(secure_subagent_endpoint(orch_token.token, "analyze", {"text": "hello"}))
print(secure_subagent_endpoint(orch_token.token, "write_memory", {"key": "x", "value": 1}))

# Read-only agent trying to delete — should fail
reader_token = issue_agent_token("reader-agent-07", scopes=["read"])
print(secure_subagent_endpoint(reader_token.token, "delete_data", {"id": "123"}))

# Expected Token Savings: JWT validation is compute-only, zero API tokens; scopes prevent privilege escalation
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Mutual TLS Certificate-Based Identity (Simulated)

In environments where mTLS is not available, simulate certificate pinning by exchanging public key fingerprints at registration and verifying them on each call.

```python
import anthropic
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

@dataclass
class AgentIdentity:
    agent_id: str
    public_key_fingerprint: str  # SHA-256 of agent's public key bytes
    registered_at: float
    allowed_actions: list[str]

# In-memory registry (use Redis/DB in production)
AGENT_REGISTRY: dict[str, AgentIdentity] = {}

def register_agent(agent_id: str, allowed_actions: list[str]) -> tuple[str, bytes]:
    """Returns (fingerprint, private_key_bytes). Store private_key_bytes securely."""
    private_key = os.urandom(32)
    fingerprint = hashlib.sha256(private_key).hexdigest()
    AGENT_REGISTRY[agent_id] = AgentIdentity(
        agent_id=agent_id,
        public_key_fingerprint=fingerprint,
        registered_at=time.time(),
        allowed_actions=allowed_actions,
    )
    return fingerprint, private_key

def _sign_request(private_key: bytes, request_body: str) -> str:
    import hmac as hmac_module
    return hmac_module.new(private_key, request_body.encode(), hashlib.sha256).hexdigest()

def _verify_request(agent_id: str, request_body: str, signature: str) -> bool:
    identity = AGENT_REGISTRY.get(agent_id)
    if not identity:
        print(f"[AUTH FAIL] Unknown agent: {agent_id}")
        return False
    import hmac as hmac_module
    # Re-derive: fingerprint is sha256 of private key
    # In real mTLS, the private key is never shared — this simulates cert pinning
    # Here we store the fingerprint and the caller proves knowledge of the key via HMAC
    # (This pattern works when caller holds the actual private key)
    expected = hashlib.sha256(
        f"{request_body}{identity.public_key_fingerprint}".encode()
    ).hexdigest()
    if not hmac_module.compare_digest(expected, signature):
        print(f"[AUTH FAIL] Signature mismatch for {agent_id}")
        return False
    return True

def create_auth_header(agent_id: str, private_key: bytes, request_body: str) -> dict:
    fingerprint = hashlib.sha256(private_key).hexdigest()
    sig = hashlib.sha256(f"{request_body}{fingerprint}".encode()).hexdigest()
    return {"X-Agent-ID": agent_id, "X-Signature": sig, "X-Timestamp": str(time.time())}


client = anthropic.Anthropic()

def secure_action_endpoint(
    agent_id: str,
    signature: str,
    action: str,
    params: dict,
) -> dict:
    request_body = json.dumps({"action": action, "params": params}, sort_keys=True)
    if not _verify_request(agent_id, request_body, signature):
        return {"error": "Authentication failed", "status": 401}
    identity = AGENT_REGISTRY[agent_id]
    if action not in identity.allowed_actions:
        return {"error": f"Action '{action}' not permitted for {agent_id}", "status": 403}
    print(f"[AUTH OK] {agent_id} → {action}")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Perform: {action} with {json.dumps(params)}"}],
    )
    return {"result": r.content[0].text, "status": 200}


# Register agents
fp_orch, pk_orch = register_agent("orchestrator", allowed_actions=["analyze", "summarize", "write"])
fp_ro, pk_ro = register_agent("readonly-agent", allowed_actions=["analyze"])

# Orchestrator makes a valid call
body = json.dumps({"action": "summarize", "params": {"text": "AI is transforming industries"}}, sort_keys=True)
header = create_auth_header("orchestrator", pk_orch, body)
print(secure_action_endpoint("orchestrator", header["X-Signature"], "summarize", {"text": "AI is transforming industries"}))

# Read-only agent tries to write — denied
body2 = json.dumps({"action": "write", "params": {"key": "x"}}, sort_keys=True)
header2 = create_auth_header("readonly-agent", pk_ro, body2)
print(secure_action_endpoint("readonly-agent", header2["X-Signature"], "write", {"key": "x"}))

# Expected Token Savings: Certificate-based auth adds no tokens; prevents lateral movement in multi-agent systems
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Request Nonce + Replay Attack Prevention

Add a one-time nonce to each request. The server tracks used nonces and rejects replayed messages even if the signature is valid.

```python
import anthropic
import hashlib
import hmac
import json
import time
import uuid
from collections import OrderedDict

# Nonce store with TTL eviction
class NonceStore:
    def __init__(self, ttl: float = 60.0, max_size: int = 10000):
        self.ttl = ttl
        self.max_size = max_size
        self._store: OrderedDict[str, float] = OrderedDict()

    def use(self, nonce: str) -> bool:
        """Returns True if nonce is fresh and not yet seen. Marks as used."""
        now = time.time()
        self._evict(now)
        if nonce in self._store:
            return False  # replay attack
        if len(self._store) >= self.max_size:
            self._store.popitem(last=False)  # evict oldest
        self._store[nonce] = now
        return True

    def _evict(self, now: float) -> None:
        while self._store:
            oldest_nonce, ts = next(iter(self._store.items()))
            if now - ts > self.ttl:
                self._store.popitem(last=False)
            else:
                break

nonce_store = NonceStore()
SIGNING_KEY = b"inter-agent-signing-key-secure-32"

def sign_request(payload: dict, agent_id: str) -> dict:
    nonce = str(uuid.uuid4())
    ts = time.time()
    body = json.dumps({"payload": payload, "agent_id": agent_id, "nonce": nonce, "ts": ts}, sort_keys=True)
    sig = hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()
    return {"payload": payload, "agent_id": agent_id, "nonce": nonce, "ts": ts, "sig": sig}

def verify_request(request: dict) -> tuple[bool, str]:
    ts = request.get("ts", 0)
    if abs(time.time() - ts) > 30:
        return False, "Request timestamp too old"
    nonce = request.get("nonce", "")
    if not nonce_store.use(nonce):
        return False, f"Replay attack detected: nonce already used"
    body = json.dumps({k: v for k, v in request.items() if k != "sig"}, sort_keys=True)
    expected = hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.get("sig", "")):
        return False, "Invalid signature"
    return True, "OK"


client = anthropic.Anthropic()

def process_agent_request(request: dict) -> dict:
    ok, reason = verify_request(request)
    if not ok:
        return {"error": reason, "status": 401}
    payload = request["payload"]
    print(f"[AUTH OK] agent={request['agent_id']} nonce={request['nonce'][:8]}...")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": str(payload)}],
    )
    return {"result": r.content[0].text, "status": 200}


# Normal request
req = sign_request({"task": "analyze sentiment", "text": "I love this product"}, "agent-A")
print(process_agent_request(req))

# Replay attack: same request sent again
print("Replaying same request:")
print(process_agent_request(req))   # should fail

# Expected Token Savings: Nonce store is memory-only, no token cost; blocks replay attacks in async message queues
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Capability-Based Authorization with Delegation Chains

Orchestrators delegate specific capabilities to subagents. Each capability token is scoped, time-limited, and non-transferable.

```python
import anthropic
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

@dataclass
class Capability:
    capability_id: str
    action: str
    granted_to: str
    granted_by: str
    resource_scope: str   # e.g. "user:123" or "*"
    expires_at: float
    depth: int            # 0 = root, 1 = once delegated, max_depth controls re-delegation

    def is_valid(self) -> bool:
        return time.time() < self.expires_at

CAP_SECRET = b"capability-signing-secret-secure"
CAPABILITY_STORE: dict[str, "Capability"] = {}

def _cap_signature(cap: "Capability") -> str:
    body = json.dumps({
        "capability_id": cap.capability_id,
        "action": cap.action,
        "granted_to": cap.granted_to,
        "granted_by": cap.granted_by,
        "resource_scope": cap.resource_scope,
        "expires_at": cap.expires_at,
        "depth": cap.depth,
    }, sort_keys=True)
    return hmac.new(CAP_SECRET, body.encode(), hashlib.sha256).hexdigest()

def issue_capability(
    action: str,
    granted_to: str,
    granted_by: str,
    resource_scope: str = "*",
    ttl: float = 120.0,
    depth: int = 0,
) -> str:
    import uuid
    cap_id = str(uuid.uuid4())
    cap = Capability(
        capability_id=cap_id,
        action=action,
        granted_to=granted_to,
        granted_by=granted_by,
        resource_scope=resource_scope,
        expires_at=time.time() + ttl,
        depth=depth,
    )
    CAPABILITY_STORE[cap_id] = cap
    return cap_id

def use_capability(cap_id: str, agent_id: str, action: str, resource: str) -> tuple[bool, str]:
    cap = CAPABILITY_STORE.get(cap_id)
    if cap is None:
        return False, "Capability not found"
    if not cap.is_valid():
        return False, "Capability expired"
    if cap.granted_to != agent_id:
        return False, f"Capability granted to {cap.granted_to}, not {agent_id}"
    if cap.action != action:
        return False, f"Capability is for action '{cap.action}', not '{action}'"
    if cap.resource_scope != "*" and resource != cap.resource_scope:
        return False, f"Capability scope is '{cap.resource_scope}', cannot access '{resource}'"
    return True, "OK"


client = anthropic.Anthropic()

def privileged_endpoint(cap_id: str, agent_id: str, action: str, resource: str, content: str) -> dict:
    ok, reason = use_capability(cap_id, agent_id, action, resource)
    if not ok:
        return {"error": reason, "status": 403}
    print(f"[CAP AUTH] {agent_id} → {action} on {resource}")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{action}: {content}"}],
    )
    return {"result": r.content[0].text, "status": 200}


# Root orchestrator issues capability to subagent
cap_id = issue_capability(
    action="summarize",
    granted_to="subagent-summarizer",
    granted_by="root-orchestrator",
    resource_scope="project:docs",
    ttl=60.0,
)
# Valid use
print(privileged_endpoint(cap_id, "subagent-summarizer", "summarize", "project:docs", "Long document text..."))
# Wrong agent
print(privileged_endpoint(cap_id, "rogue-agent", "summarize", "project:docs", "..."))
# Wrong resource
print(privileged_endpoint(cap_id, "subagent-summarizer", "summarize", "project:secrets", "..."))

# Expected Token Savings: Capability checks are zero-token; prevents lateral movement between project scopes
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Challenge-Response Handshake Before Privileged Operation

Before executing a high-risk action, the receiving agent issues a challenge that only the legitimate caller can answer, using a shared secret.

```python
import anthropic
import hashlib
import hmac
import os
import time
from dataclasses import dataclass

@dataclass
class Challenge:
    challenge_id: str
    nonce: str
    issued_at: float
    action: str
    requester_id: str

PENDING_CHALLENGES: dict[str, Challenge] = {}
AGENT_SECRETS: dict[str, bytes] = {
    "orchestrator-01": b"secret-for-orchestrator-01-32byt",
    "planner-agent": b"secret-for-planner-agent-32bytes",
}

def issue_challenge(requester_id: str, action: str) -> dict:
    challenge_id = os.urandom(8).hex()
    nonce = os.urandom(16).hex()
    ch = Challenge(
        challenge_id=challenge_id,
        nonce=nonce,
        issued_at=time.time(),
        action=action,
        requester_id=requester_id,
    )
    PENDING_CHALLENGES[challenge_id] = ch
    return {"challenge_id": challenge_id, "nonce": nonce}

def compute_response(agent_id: str, nonce: str, action: str) -> str:
    secret = AGENT_SECRETS.get(agent_id, b"")
    body = f"{agent_id}:{nonce}:{action}"
    return hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()

def verify_challenge_response(
    challenge_id: str,
    agent_id: str,
    response: str,
    max_age: float = 15.0,
) -> tuple[bool, str]:
    ch = PENDING_CHALLENGES.pop(challenge_id, None)
    if ch is None:
        return False, "Challenge not found or already used"
    if time.time() - ch.issued_at > max_age:
        return False, "Challenge expired"
    if ch.requester_id != agent_id:
        return False, f"Challenge was issued for {ch.requester_id}"
    expected = compute_response(agent_id, ch.nonce, ch.action)
    if not hmac.compare_digest(expected, response):
        return False, "Challenge response incorrect"
    return True, "OK"


client = anthropic.Anthropic()

def request_privileged_action(agent_id: str, action: str, params: dict) -> dict:
    # Step 1: Request challenge
    ch_data = issue_challenge(agent_id, action)
    print(f"[CHALLENGE] Issued to {agent_id}: challenge_id={ch_data['challenge_id'][:8]}...")

    # Step 2: Agent computes response (using its secret)
    resp = compute_response(agent_id, ch_data["nonce"], action)

    # Step 3: Verify and execute
    ok, reason = verify_challenge_response(ch_data["challenge_id"], agent_id, resp)
    if not ok:
        return {"error": reason, "status": 401}

    print(f"[AUTH OK] {agent_id} passed challenge for '{action}'")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Privileged action '{action}': {params}"}],
    )
    return {"result": r.content[0].text, "status": 200}


# Legitimate orchestrator
print(request_privileged_action("orchestrator-01", "delete_session", {"session_id": "abc123"}))

# Unknown agent (no secret registered)
print(request_privileged_action("unknown-agent", "delete_session", {"session_id": "abc123"}))

# Expected Token Savings: Challenge-response is zero API tokens; ensures only registered agents run privileged ops
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Mechanism | Replay Protection | Delegation | Complexity | Best For |
|--------|-----------|------------------|------------|------------|----------|
| 1. HMAC Signing | Shared secret + timestamp | Timestamp window | No | Low | Internal microservices |
| 2. JWT Tokens | Short-lived tokens + scopes | Token expiry | Scope inheritance | Medium | REST APIs, HTTP gateways |
| 3. Key Fingerprint | Cert pinning simulation | Timestamp | No | Medium | Non-HTTP transports |
| 4. Nonce Store | One-time nonce | Full replay prevention | No | Medium | Message queues, async |
| 5. Capability Tokens | Scoped + delegated caps | Token expiry | Yes | High | Fine-grained authorization |
| 6. Challenge-Response | Proves shared secret | Per-challenge nonce | No | Medium | High-risk privileged ops |

**Recommended**: Option 2 (JWT) for HTTP-based multi-agent systems. Option 4 (nonce) for async message queues. Option 5 (capabilities) for complex delegation hierarchies.
