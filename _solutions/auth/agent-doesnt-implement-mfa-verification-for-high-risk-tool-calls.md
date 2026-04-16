---
layout: solution
title: "Agent Doesn't Implement MFA Verification for High-Risk Tool Calls"
category: auth
description: "Agent executes high-risk actions (delete account, wire transfer, deploy to production) based solely on session token, with no step-up authentication for elevated privilege operations."
tags: [auth, mfa, step-up-auth, security, high-risk-actions]
---

# Agent Doesn't Implement MFA Verification for High-Risk Tool Calls

## Problem

When an agent has access to high-risk tools — deleting user data, initiating financial transfers, deploying infrastructure changes — relying solely on the initial session token is insufficient. If a session is hijacked or a user is socially engineered, the agent will execute destructive actions without any additional verification. Step-up authentication (MFA challenge at the point of risky tool invocation) provides a second gate that stops unauthorized actions even when the primary session is compromised.

## Solution Options

### Option 1: Risk-Scored Tool Gating with OTP Verification

```python
import anthropic
import json
import random
import string
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class RiskLevel(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

# Risk classification for each tool
TOOL_RISK_LEVELS: dict[str, RiskLevel] = {
    "get_user_info": RiskLevel.LOW,
    "search_database": RiskLevel.LOW,
    "update_preferences": RiskLevel.MEDIUM,
    "send_email": RiskLevel.MEDIUM,
    "delete_file": RiskLevel.HIGH,
    "transfer_funds": RiskLevel.CRITICAL,
    "delete_account": RiskLevel.CRITICAL,
    "deploy_to_production": RiskLevel.CRITICAL,
    "grant_admin_access": RiskLevel.CRITICAL,
}

MFA_REQUIRED_THRESHOLD = RiskLevel.HIGH

@dataclass
class MFAState:
    pending_otp: str = ""
    otp_expires_at: float = 0.0
    verified_tools: set[str] = field(default_factory=set)
    verification_window: float = 300.0  # 5 minutes — once verified, no re-challenge

    def generate_otp(self) -> str:
        self.pending_otp = "".join(random.choices(string.digits, k=6))
        self.otp_expires_at = time.time() + 120  # 2-minute expiry
        return self.pending_otp

    def verify_otp(self, submitted: str) -> bool:
        if time.time() > self.otp_expires_at:
            return False
        return submitted.strip() == self.pending_otp

    def mark_verified(self, tool_name: str):
        self.verified_tools.add(tool_name)

    def is_recently_verified(self, tool_name: str) -> bool:
        return tool_name in self.verified_tools

def send_otp_to_user(otp: str, user_id: str):
    """In production: send via SMS, authenticator app, or email."""
    print(f"[MFA] OTP sent to user {user_id}: {otp} (expires in 2 minutes)")

def prompt_user_for_otp(tool_name: str) -> str:
    """In production: suspend agent, return challenge to frontend, await user response."""
    print(f"[MFA CHALLENGE] '{tool_name}' requires verification.")
    # In a real system this would suspend and wait for async user input
    return input("Enter OTP: ").strip()

mfa_state = MFAState()

def check_mfa_gate(tool_name: str, user_id: str = "u42") -> bool:
    """Returns True if the tool call is authorized to proceed."""
    risk = TOOL_RISK_LEVELS.get(tool_name, RiskLevel.LOW)

    if risk.value < MFA_REQUIRED_THRESHOLD.value:
        return True  # Low/medium risk: no MFA required

    if mfa_state.is_recently_verified(tool_name):
        print(f"[MFA] {tool_name} — using cached verification")
        return True

    # Generate and send OTP
    otp = mfa_state.generate_otp()
    send_otp_to_user(otp, user_id)

    # Simulate auto-approval in demo (in production: block and await user)
    submitted = otp  # Auto-fill for demo; replace with: prompt_user_for_otp(tool_name)
    print(f"[MFA] User submitted OTP: {submitted}")

    if mfa_state.verify_otp(submitted):
        mfa_state.mark_verified(tool_name)
        print(f"[MFA] Verified — {tool_name} authorized")
        return True
    else:
        print(f"[MFA] FAILED — {tool_name} blocked")
        return False

def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Execute tool after MFA gate check."""
    if not check_mfa_gate(tool_name):
        return {"error": "MFA verification failed — action blocked", "tool": tool_name}

    # Simulate tool execution
    print(f"[TOOL EXECUTE] {tool_name}({json.dumps(tool_input)})")
    return {"status": "success", "tool": tool_name, "result": f"executed_{tool_name}"}

tools = [
    {"name": "get_user_info", "description": "Get user info (low risk)", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    {"name": "transfer_funds", "description": "Transfer money (CRITICAL - MFA required)", "input_schema": {"type": "object", "properties": {"amount": {"type": "number"}, "to": {"type": "string"}}, "required": ["amount", "to"]}},
    {"name": "delete_account", "description": "Delete user account (CRITICAL - MFA required)", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
]

messages = [{"role": "user", "content": "Get my info (user u42), then transfer $500 to account 9900."}]

while True:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        messages=messages,
    )

    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            risk = TOOL_RISK_LEVELS.get(block.name, RiskLevel.LOW)
            print(f"\n[TOOL REQUEST] {block.name} (risk={risk.name})")
            result = execute_tool(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — MFA is a security gate, not a cost optimization
# Environment: Financial, healthcare, or admin agents with destructive tool capabilities
```

### Option 2: TOTP-Based Step-Up Authentication

```python
import anthropic
import json
import math
import time
import struct
import hashlib
import hmac
from dataclasses import dataclass, field

client = anthropic.Anthropic()

def generate_totp(secret: bytes, window: int = 0, step: int = 30, digits: int = 6) -> str:
    """Generate a TOTP code (RFC 6238 compliant)."""
    timestamp = int(time.time()) // step + window
    msg = struct.pack(">Q", timestamp)
    h = hmac.new(secret, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)

def verify_totp(secret: bytes, submitted: str, tolerance: int = 1) -> bool:
    """Verify TOTP with ±tolerance windows for clock skew."""
    for window in range(-tolerance, tolerance + 1):
        if generate_totp(secret, window) == submitted:
            return True
    return False

@dataclass
class TOTPSession:
    user_id: str
    totp_secret: bytes  # In production: per-user secret stored securely
    verified_actions: dict[str, float] = field(default_factory=dict)
    verification_ttl: float = 300.0

    def needs_verification(self, action: str) -> bool:
        last_verified = self.verified_actions.get(action, 0)
        return time.time() - last_verified > self.verification_ttl

    def mark_verified(self, action: str):
        self.verified_actions[action] = time.time()

# High-risk action categories
HIGH_RISK_ACTIONS = {
    "delete_records", "transfer_funds", "modify_permissions",
    "deploy_code", "export_data", "change_password",
}

def step_up_auth(session: TOTPSession, action: str) -> bool:
    """Perform TOTP step-up authentication for a high-risk action."""
    if action not in HIGH_RISK_ACTIONS:
        return True

    if not session.needs_verification(action):
        print(f"[STEP-UP] {action} — cached verification still valid")
        return True

    # Generate current valid code (what the user's authenticator app shows)
    current_code = generate_totp(session.totp_secret)
    print(f"[STEP-UP REQUIRED] Action '{action}' requires TOTP verification")
    print(f"[DEMO] Current valid TOTP: {current_code}")

    # In production: receive code from user via API; here we simulate
    submitted = current_code  # Simulate correct user input

    if verify_totp(session.totp_secret, submitted):
        session.mark_verified(action)
        print(f"[STEP-UP OK] {action} authorized via TOTP")
        return True
    else:
        print(f"[STEP-UP DENIED] Invalid TOTP for {action}")
        return False

# Simulate user with a TOTP secret (stored in their authenticator app)
import os
session = TOTPSession(
    user_id="alice",
    totp_secret=b"ALICE_SECRET_KEY_32BYTES_LONGXX",  # In production: random per-user
)

tools = [
    {"name": "get_balance", "description": "Get account balance", "input_schema": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]}},
    {"name": "transfer_funds", "description": "Transfer funds between accounts", "input_schema": {"type": "object", "properties": {"from_id": {"type": "string"}, "to_id": {"type": "string"}, "amount": {"type": "number"}}, "required": ["from_id", "to_id", "amount"]}},
    {"name": "export_data", "description": "Export account data", "input_schema": {"type": "object", "properties": {"format": {"type": "string"}}, "required": ["format"]}},
]

ACTION_MAP = {"transfer_funds": "transfer_funds", "export_data": "export_data"}

messages = [{"role": "user", "content": "Check my balance for account ACC001 and transfer $200 to ACC002."}]

while True:
    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            action = ACTION_MAP.get(block.name, "")
            if action and not step_up_auth(session, action):
                result = {"error": f"Step-up authentication required for {block.name}", "blocked": True}
            else:
                result = {"status": "ok", "tool": block.name, "executed": True}
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — TOTP step-up is a security control
# Environment: Financial and administrative agents where TOTP authenticator apps are in use
```

### Option 3: Risk-Based Approval Workflow with Human-in-the-Loop

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ApprovalRequest:
    request_id: str
    tool_name: str
    tool_input: dict
    risk_reason: str
    status: str = "pending"  # pending, approved, denied, expired
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

class ApprovalQueue:
    """Simulates an async approval queue (in production: backed by database + notifications)."""

    def __init__(self):
        self._queue: dict[str, ApprovalRequest] = {}

    def submit(self, tool_name: str, tool_input: dict, risk_reason: str) -> str:
        req_id = str(uuid.uuid4())[:8]
        req = ApprovalRequest(
            request_id=req_id,
            tool_name=tool_name,
            tool_input=tool_input,
            risk_reason=risk_reason,
        )
        self._queue[req_id] = req
        print(f"\n[APPROVAL REQUEST #{req_id}]")
        print(f"  Tool: {tool_name}")
        print(f"  Input: {json.dumps(tool_input)}")
        print(f"  Risk: {risk_reason}")
        print(f"  Action required: Approve at /approvals/{req_id}")
        return req_id

    def approve(self, req_id: str) -> bool:
        req = self._queue.get(req_id)
        if not req or req.is_expired:
            return False
        req.status = "approved"
        return True

    def deny(self, req_id: str, reason: str = "") -> bool:
        req = self._queue.get(req_id)
        if not req:
            return False
        req.status = "denied"
        return True

    def get_status(self, req_id: str) -> str:
        req = self._queue.get(req_id)
        if not req:
            return "not_found"
        if req.is_expired:
            req.status = "expired"
        return req.status

    def wait_for_approval(self, req_id: str, timeout: float = 30.0, poll_interval: float = 0.5) -> str:
        """Poll until approved/denied/expired (simulated)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_status(req_id)
            if status != "pending":
                return status
            time.sleep(poll_interval)
        return "timeout"

# Define which tools require human approval
APPROVAL_REQUIRED_TOOLS: dict[str, str] = {
    "delete_database_records": "Irreversible data deletion",
    "wire_transfer": "Financial transaction above threshold",
    "revoke_api_keys": "Security credential revocation",
    "terminate_instances": "Infrastructure destruction",
    "mass_email_send": "Bulk communication with users",
}

approval_queue = ApprovalQueue()

def execute_with_approval(tool_name: str, tool_input: dict) -> dict:
    """Gate high-risk tools behind human approval."""
    risk_reason = APPROVAL_REQUIRED_TOOLS.get(tool_name)

    if not risk_reason:
        # Low-risk: execute immediately
        print(f"[EXECUTE] {tool_name} (no approval needed)")
        return {"status": "executed", "tool": tool_name}

    # Submit for approval
    req_id = approval_queue.submit(tool_name, tool_input, risk_reason)

    # In production: return request_id to caller, suspend agent, resume on webhook
    # In this demo: auto-approve after a brief "review" delay
    print(f"[SIMULATION] Auto-approving request #{req_id} after 1s review...")
    time.sleep(1)
    approval_queue.approve(req_id)

    status = approval_queue.wait_for_approval(req_id, timeout=10)
    print(f"[APPROVAL] Request #{req_id} status: {status}")

    if status == "approved":
        print(f"[EXECUTE] {tool_name} approved — executing")
        return {"status": "executed", "tool": tool_name, "approval_id": req_id}
    else:
        return {"status": "blocked", "reason": status, "tool": tool_name, "approval_id": req_id}

tools = [
    {"name": "get_user_count", "description": "Get user count (safe)", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "delete_database_records", "description": "Delete records matching filter (APPROVAL REQUIRED)", "input_schema": {"type": "object", "properties": {"filter": {"type": "string"}, "table": {"type": "string"}}, "required": ["filter", "table"]}},
    {"name": "wire_transfer", "description": "Execute wire transfer (APPROVAL REQUIRED)", "input_schema": {"type": "object", "properties": {"amount": {"type": "number"}, "recipient": {"type": "string"}}, "required": ["amount", "recipient"]}},
]

messages = [{"role": "user", "content": "Get the user count, then delete all inactive users from the users table (filter: status='inactive')."}]

while True:
    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_with_approval(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — approval workflow adds latency for safety, not optimization
# Environment: Agents performing irreversible actions requiring manager or compliance approval
```

### Option 4: Session Trust Level with Automatic Step-Up

```python
import anthropic
import json
import time
from dataclasses import dataclass
from enum import IntEnum

client = anthropic.Anthropic()

class TrustLevel(IntEnum):
    ANONYMOUS = 0
    AUTHENTICATED = 1
    MFA_VERIFIED = 2
    PRIVILEGED = 3

@dataclass
class UserSession:
    user_id: str
    trust_level: TrustLevel
    trust_elevated_at: float = 0.0
    elevation_ttl: float = 600.0  # 10 minutes

    @property
    def effective_trust(self) -> TrustLevel:
        """Trust level degrades back after TTL."""
        if self.trust_level >= TrustLevel.MFA_VERIFIED:
            elapsed = time.time() - self.trust_elevated_at
            if elapsed > self.elevation_ttl:
                return TrustLevel.AUTHENTICATED
        return self.trust_level

    def elevate(self, new_level: TrustLevel):
        self.trust_level = new_level
        self.trust_elevated_at = time.time()
        print(f"[TRUST] Session elevated to {new_level.name} (expires in {self.elevation_ttl:.0f}s)")

# Tool-to-minimum-trust-level mapping
TOOL_MIN_TRUST: dict[str, TrustLevel] = {
    "read_profile": TrustLevel.AUTHENTICATED,
    "update_email": TrustLevel.MFA_VERIFIED,
    "change_password": TrustLevel.MFA_VERIFIED,
    "download_data": TrustLevel.MFA_VERIFIED,
    "delete_account": TrustLevel.PRIVILEGED,
    "access_billing": TrustLevel.MFA_VERIFIED,
    "add_payment_method": TrustLevel.PRIVILEGED,
}

def simulate_mfa_challenge(session: UserSession, required_level: TrustLevel) -> bool:
    """Simulate MFA challenge and elevation."""
    print(f"[MFA CHALLENGE] Session trust={session.effective_trust.name}, required={required_level.name}")

    if required_level == TrustLevel.MFA_VERIFIED:
        # Simulate TOTP/SMS verification
        print(f"[MFA] Sending verification code to user {session.user_id}...")
        time.sleep(0.1)  # Simulate round-trip
        verified = True  # In production: await user response
        if verified:
            session.elevate(TrustLevel.MFA_VERIFIED)
            return True
    elif required_level == TrustLevel.PRIVILEGED:
        # Require explicit manager approval or hardware key
        print(f"[MFA] PRIVILEGED action requires hardware key or manager approval")
        approved = True  # In production: hardware key attestation
        if approved:
            session.elevate(TrustLevel.PRIVILEGED)
            return True

    return False

def trust_gated_tool(session: UserSession, tool_name: str, tool_input: dict) -> dict:
    min_trust = TOOL_MIN_TRUST.get(tool_name, TrustLevel.AUTHENTICATED)
    effective = session.effective_trust

    print(f"\n[TRUST GATE] {tool_name}: required={min_trust.name}, current={effective.name}")

    if effective < min_trust:
        # Attempt step-up
        print(f"[STEP-UP] Insufficient trust — attempting elevation")
        success = simulate_mfa_challenge(session, min_trust)
        if not success:
            return {"error": f"Insufficient trust level for {tool_name}", "blocked": True}

    print(f"[AUTHORIZED] Executing {tool_name}")
    return {"status": "success", "tool": tool_name, "input": tool_input}

# Session: user is authenticated but not MFA-verified yet
session = UserSession(
    user_id="user_789",
    trust_level=TrustLevel.AUTHENTICATED,
)

tools = [
    {"name": "read_profile", "description": "Read user profile", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "update_email", "description": "Update email address", "input_schema": {"type": "object", "properties": {"new_email": {"type": "string"}}, "required": ["new_email"]}},
    {"name": "delete_account", "description": "Delete account permanently", "input_schema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]}},
]

messages = [{"role": "user", "content": "Read my profile, then update my email to new@example.com."}]

while True:
    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = trust_gated_tool(session, block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — trust gating is a security control with TTL-based re-verification
# Environment: SaaS platforms with tiered trust levels and MFA-enrolled users
```

### Option 5: Cryptographic Action Signing with Time-Limited Tokens

```python
import anthropic
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

client = anthropic.Anthropic()

# In production: derive per-user, store in HSM or secrets manager
SIGNING_SECRET = b"super_secret_action_signing_key_32b"

def sign_action(tool_name: str, tool_input: dict, user_id: str, expires_in: float = 120) -> str:
    """Generate a signed action token valid for `expires_in` seconds."""
    payload = {
        "tool": tool_name,
        "input_hash": hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:16],
        "user": user_id,
        "exp": int(time.time() + expires_in),
        "jti": str(uuid.uuid4())[:8],
    }
    payload_str = json.dumps(payload, sort_keys=True)
    signature = hmac.new(SIGNING_SECRET, payload_str.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload_str}|{signature}"

def verify_action_token(token: str, tool_name: str, tool_input: dict, user_id: str) -> bool:
    """Verify that the action token is valid, not expired, and matches the tool call."""
    try:
        parts = token.rsplit("|", 1)
        if len(parts) != 2:
            return False
        payload_str, submitted_sig = parts
        payload = json.loads(payload_str)

        # Verify signature
        expected_sig = hmac.new(SIGNING_SECRET, payload_str.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expected_sig, submitted_sig):
            print("[SIGN] Invalid signature")
            return False

        # Verify expiry
        if time.time() > payload["exp"]:
            print("[SIGN] Token expired")
            return False

        # Verify tool name and user match
        if payload["tool"] != tool_name or payload["user"] != user_id:
            print("[SIGN] Tool or user mismatch")
            return False

        # Verify input hash matches
        expected_hash = hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:16]
        if payload["input_hash"] != expected_hash:
            print("[SIGN] Input hash mismatch — action parameters were altered!")
            return False

        return True
    except Exception as e:
        print(f"[SIGN] Verification error: {e}")
        return False

# Issued tokens storage (in production: Redis or DB with TTL)
_pending_tokens: dict[str, str] = {}

SIGNING_REQUIRED_TOOLS = {"wire_transfer", "delete_records", "provision_infrastructure"}

def request_signed_action(user_id: str, tool_name: str, tool_input: dict) -> str:
    """Issue a signed token for a high-risk action after MFA verification."""
    # In production: verify MFA here before issuing token
    print(f"[SIGN] Issuing signed token for {tool_name} (user={user_id})")
    token = sign_action(tool_name, tool_input, user_id)
    action_key = f"{user_id}:{tool_name}"
    _pending_tokens[action_key] = token
    return token

def execute_signed_tool(user_id: str, tool_name: str, tool_input: dict) -> dict:
    if tool_name not in SIGNING_REQUIRED_TOOLS:
        print(f"[EXEC] {tool_name} — no signing required")
        return {"status": "executed", "tool": tool_name}

    action_key = f"{user_id}:{tool_name}"
    token = _pending_tokens.get(action_key)

    if not token:
        # Generate one (in production: user must request this via authenticated endpoint)
        token = request_signed_action(user_id, tool_name, tool_input)

    if verify_action_token(token, tool_name, tool_input, user_id):
        del _pending_tokens[action_key]  # One-time use
        print(f"[EXEC] {tool_name} — cryptographically authorized")
        return {"status": "executed", "tool": tool_name}
    else:
        return {"status": "blocked", "reason": "invalid_or_expired_signature", "tool": tool_name}

tools = [
    {"name": "get_balance", "description": "Get balance", "input_schema": {"type": "object", "properties": {"account": {"type": "string"}}, "required": ["account"]}},
    {"name": "wire_transfer", "description": "Wire transfer (signed action required)", "input_schema": {"type": "object", "properties": {"amount": {"type": "number"}, "to_account": {"type": "string"}}, "required": ["amount", "to_account"]}},
]

USER_ID = "alice_123"
messages = [{"role": "user", "content": "Check balance of ACC100 and wire $1000 to ACC200."}]

while True:
    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_signed_tool(USER_ID, block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — cryptographic signing provides non-repudiation and integrity
# Environment: Financial and compliance-sensitive agents with audit trail requirements
```

### Option 6: Velocity-Based Automatic MFA Re-Challenge

```python
import anthropic
import json
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class VelocityMonitor:
    """Detects anomalous action velocity and triggers re-authentication."""
    user_id: str
    max_high_risk_per_hour: int = 3
    max_total_per_minute: int = 10

    _high_risk_timestamps: deque = field(default_factory=deque)
    _all_timestamps: deque = field(default_factory=deque)
    _mfa_verified_at: float = 0.0
    _mfa_ttl: float = 300.0

    HIGH_RISK_TOOLS = {"delete_record", "transfer", "grant_access", "revoke_keys"}

    def record_action(self, tool_name: str):
        now = time.time()
        self._all_timestamps.append(now)
        if tool_name in self.HIGH_RISK_TOOLS:
            self._high_risk_timestamps.append(now)
        self._evict_old()

    def _evict_old(self):
        now = time.time()
        while self._all_timestamps and now - self._all_timestamps[0] > 60:
            self._all_timestamps.popleft()
        while self._high_risk_timestamps and now - self._high_risk_timestamps[0] > 3600:
            self._high_risk_timestamps.popleft()

    def is_anomalous(self) -> tuple[bool, str]:
        self._evict_old()
        if len(self._high_risk_timestamps) >= self.max_high_risk_per_hour:
            return True, f"High-risk velocity: {len(self._high_risk_timestamps)}/{self.max_high_risk_per_hour} per hour"
        if len(self._all_timestamps) >= self.max_total_per_minute:
            return True, f"Total velocity: {len(self._all_timestamps)}/{self.max_total_per_minute} per minute"
        return False, ""

    def is_mfa_valid(self) -> bool:
        return time.time() - self._mfa_verified_at < self._mfa_ttl

    def verify_mfa(self) -> bool:
        """Simulate MFA verification."""
        print(f"[MFA CHALLENGE] Anomalous velocity detected — re-authentication required")
        time.sleep(0.1)  # Simulate round-trip
        self._mfa_verified_at = time.time()
        print(f"[MFA OK] Re-authentication successful")
        return True

def velocity_gated_call(monitor: VelocityMonitor, tool_name: str, tool_input: dict) -> dict:
    anomalous, reason = monitor.is_anomalous()

    if anomalous and not monitor.is_mfa_valid():
        print(f"[VELOCITY ALERT] {reason}")
        if not monitor.verify_mfa():
            return {"blocked": True, "reason": "MFA verification failed"}

    monitor.record_action(tool_name)
    print(f"[EXEC] {tool_name} — velocity ok ({len(monitor._high_risk_timestamps)} high-risk in window)")
    return {"status": "executed", "tool": tool_name}

monitor = VelocityMonitor(user_id="bob", max_high_risk_per_hour=3, max_total_per_minute=10)

tools = [
    {"name": "delete_record", "description": "Delete a record", "input_schema": {"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]}},
    {"name": "transfer", "description": "Transfer funds", "input_schema": {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]}},
]

messages = [{"role": "user", "content": "Delete records R1, R2, R3, and R4. Then transfer $100."}]

while True:
    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
    if response.stop_reason == "end_turn":
        print(f"\nAgent: {next((b.text for b in response.content if hasattr(b, 'text')), '')}")
        break
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = velocity_gated_call(monitor, block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

# Expected Token Savings: None — velocity monitoring is a fraud detection control
# Environment: Agents used in financial/admin contexts vulnerable to session hijacking or prompt injection attacks
```

## Comparison

| Option | Auth Method | Human Required | Cacheable | Audit Trail | Best For |
|--------|------------|---------------|-----------|------------|---------|
| 1. OTP Risk Scoring | SMS/Email OTP | No | Yes (5 min TTL) | Partial | General MFA for high-risk tools |
| 2. TOTP Step-Up | Authenticator app | No | Yes (5 min TTL) | No | Teams with TOTP authenticator enrollment |
| 3. Human Approval | Manager approval | Yes | No | Yes | Irreversible actions needing audit trail |
| 4. Trust Level | MFA + trust tier | No | Yes (TTL) | No | SaaS with tiered trust architecture |
| 5. Crypto Signing | HMAC token | No | No (one-time) | Yes | Compliance/financial with non-repudiation |
| 6. Velocity Gating | Re-authentication | No | Yes (TTL) | Partial | Fraud detection and anomaly response |
