---
layout: solution
title: "Agent Doesn't Implement Dynamic Instruction Injection by User Role"
category: general
description: "Augment the system prompt dynamically based on authenticated user role, permissions, and context—enabling a single agent to serve admin, standard, and guest users with appropriately scoped capabilities."
tags: [rbac, instruction-injection, multi-tenant, system-prompt, role-based]
---

# Agent Doesn't Implement Dynamic Instruction Injection by User Role

## Problem

A single system prompt cannot serve all user types securely. Admins need access to privileged operations, standard users need guardrails, guests need restricted capabilities—but without dynamic injection, agents either over-permit (security risk) or under-permit (poor UX) for all users.

## Solution Options

### Option 1: Role-Based System Prompt Compositor

```python
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class UserRole(Enum):
    ADMIN = "admin"
    STANDARD = "standard"
    GUEST = "guest"
    ANALYST = "analyst"

BASE_INSTRUCTIONS = """You are a helpful AI assistant for Acme Corp's internal platform.
Always be professional and accurate."""

ROLE_INSTRUCTIONS = {
    UserRole.ADMIN: """
ADMIN PRIVILEGES ACTIVE:
- You can discuss internal system configurations and database schemas.
- You may access and explain privileged operations.
- You can authorize actions that affect multiple users.
- If asked for audit logs or user data, provide them without redaction.""",

    UserRole.STANDARD: """
STANDARD USER ACCESS:
- Assist with general tasks and questions.
- Do NOT reveal internal system configurations or other users' data.
- For actions that affect billing or account settings, require confirmation first.
- Escalate requests for privileged operations to the user's admin.""",

    UserRole.ANALYST: """
ANALYST ACCESS:
- You can discuss aggregated data, metrics, and reports.
- Do NOT reveal individual user PII.
- You may run and explain SQL queries on anonymized datasets.
- You cannot modify data or execute write operations.""",

    UserRole.GUEST: """
GUEST ACCESS (LIMITED):
- Answer only general product documentation questions.
- Do NOT discuss pricing, internal architecture, or user data.
- For account-specific questions, direct the user to sign in.
- You cannot execute any operations — view-only assistance."""
}

@dataclass
class AuthenticatedUser:
    user_id: str
    role: UserRole
    name: str
    department: str | None = None

def build_system_prompt(user: AuthenticatedUser) -> str:
    role_block = ROLE_INSTRUCTIONS.get(user.role, ROLE_INSTRUCTIONS[UserRole.GUEST])
    context_block = f"\nCurrent user: {user.name} (ID: {user.user_id}, Role: {user.role.value})"
    if user.department:
        context_block += f", Department: {user.department}"
    return BASE_INSTRUCTIONS + role_block + context_block

def ask_as_user(user: AuthenticatedUser, question: str) -> str:
    system = build_system_prompt(user)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

# Test same question across different roles
question = "Can you show me the database connection configuration?"
users = [
    AuthenticatedUser("u001", UserRole.ADMIN, "Alice", "Engineering"),
    AuthenticatedUser("u002", UserRole.STANDARD, "Bob", "Sales"),
    AuthenticatedUser("u003", UserRole.GUEST, "Charlie"),
]

for user in users:
    reply = ask_as_user(user, question)
    print(f"[{user.role.value.upper()}] {user.name}: {reply[:100]}...\n")

# Expected Token Savings: single agent handles all roles; no per-role model deployment
# Environment: multi-tenant SaaS, enterprise internal tools, permission-scoped assistants
```

### Option 2: Permission-Scoped Tool Availability Injection

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

ALL_TOOLS = [
    {
        "name": "read_user_data",
        "description": "Read a user's profile and account data",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}
    },
    {
        "name": "modify_user_data",
        "description": "Update a user's account settings",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}, "field": {"type": "string"}, "value": {"type": "string"}}, "required": ["user_id", "field", "value"]}
    },
    {
        "name": "delete_user",
        "description": "Permanently delete a user account",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["user_id", "reason"]}
    },
    {
        "name": "view_analytics",
        "description": "View aggregated usage analytics",
        "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}, "required": ["metric"]}
    }
]

ROLE_TOOL_PERMISSIONS = {
    "admin":    {"read_user_data", "modify_user_data", "delete_user", "view_analytics"},
    "analyst":  {"read_user_data", "view_analytics"},
    "standard": {"read_user_data"},
    "guest":    set()
}

ROLE_SYSTEM_ADDITIONS = {
    "admin":    "You have full administrative access. All tools are available.",
    "analyst":  "You have read-only data access. You cannot modify or delete records.",
    "standard": "You can only view your own account data. No modification capabilities.",
    "guest":    "No tool access. Explain what sign-in would unlock."
}

@dataclass
class UserContext:
    user_id: str
    role: str

def get_permitted_tools(role: str) -> list[dict]:
    permitted_names = ROLE_TOOL_PERMISSIONS.get(role, set())
    return [t for t in ALL_TOOLS if t["name"] in permitted_names]

def agent_call(user: UserContext, request: str) -> str:
    permitted_tools = get_permitted_tools(user.role)
    system = (
        f"You are an account management assistant. User: {user.user_id} (role: {user.role}). "
        f"{ROLE_SYSTEM_ADDITIONS.get(user.role, '')} "
        f"Available tools: {[t['name'] for t in permitted_tools] or 'none'}."
    )
    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "system": system,
        "messages": [{"role": "user", "content": request}]
    }
    if permitted_tools:
        kwargs["tools"] = permitted_tools

    resp = client.messages.create(**kwargs)
    return resp.content[0].text if resp.content and hasattr(resp.content[0], 'text') else str(resp.content[0])

test_request = "Please delete user account u_99 and show me analytics for page views."
for role in ["admin", "analyst", "standard", "guest"]:
    user = UserContext(user_id="u_001", role=role)
    reply = agent_call(user, test_request)
    tools = get_permitted_tools(role)
    print(f"[{role.upper()}] tools={[t['name'] for t in tools]}")
    print(f"  {reply[:100]}...\n")

# Expected Token Savings: guest gets zero tool overhead; tool schemas only sent when permitted
# Environment: enterprise platforms, admin panels, permission-critical agent deployments
```

### Option 3: Contextual Constraint Injection from JWT Claims

```python
import anthropic
import json
import base64
from dataclasses import dataclass

client = anthropic.Anthropic()

# Simulated JWT payload (normally decoded from Bearer token)
MOCK_JWT_CLAIMS = {
    "user_001_admin": {
        "sub": "user_001",
        "name": "Alice Admin",
        "roles": ["admin", "engineer"],
        "permissions": ["read:all", "write:all", "delete:users"],
        "dept": "Engineering",
        "max_token_budget": 10000,
        "allowed_models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]
    },
    "user_002_standard": {
        "sub": "user_002",
        "name": "Bob User",
        "roles": ["standard"],
        "permissions": ["read:own", "write:own"],
        "dept": "Marketing",
        "max_token_budget": 2000,
        "allowed_models": ["claude-haiku-4-5-20251001"]
    },
}

@dataclass
class ClaimsContext:
    sub: str
    name: str
    roles: list[str]
    permissions: list[str]
    dept: str
    max_token_budget: int
    allowed_models: list[str]

def parse_claims(token_id: str) -> ClaimsContext:
    raw = MOCK_JWT_CLAIMS.get(token_id, MOCK_JWT_CLAIMS["user_002_standard"])
    return ClaimsContext(**raw)

def build_instructions_from_claims(claims: ClaimsContext) -> str:
    lines = [
        f"Authenticated user: {claims.name} ({claims.sub}), Department: {claims.dept}",
        f"Roles: {', '.join(claims.roles)}",
        f"Permitted actions: {', '.join(claims.permissions)}",
    ]

    if "delete:users" in claims.permissions:
        lines.append("You MAY assist with user deletion requests after confirming intent.")
    else:
        lines.append("You MUST NOT assist with deletion or destructive operations.")

    if "read:all" in claims.permissions:
        lines.append("You MAY discuss any system data including other users' information.")
    elif "read:own" in claims.permissions:
        lines.append("You MAY ONLY discuss data belonging to this specific user.")

    lines.append(f"Response token budget for this user: {claims.max_token_budget}")
    lines.append(f"Approved models: {', '.join(claims.allowed_models)}")

    return "\n".join(lines)

def authenticated_call(token_id: str, user_message: str) -> str:
    claims = parse_claims(token_id)
    model = claims.allowed_models[0]  # Use highest-permitted model available
    max_tokens = min(512, claims.max_token_budget)

    system = (
        "You are a secure enterprise assistant. Strictly enforce the user's permission boundaries.\n\n"
        + build_instructions_from_claims(claims)
    )

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return resp.content[0].text

question = "Show me all user accounts and delete any inactive ones."
for token_id in ["user_001_admin", "user_002_standard"]:
    claims = parse_claims(token_id)
    reply = authenticated_call(token_id, question)
    print(f"[{claims.name} / {claims.roles}]")
    print(f"  {reply[:120]}...\n")

# Expected Token Savings: token budget from JWT prevents runaway costs per user tier
# Environment: JWT-authenticated APIs, enterprise SSO, permission-driven agent endpoints
```

### Option 4: Progressive Capability Unlock Based on Session State

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class SessionTrust(Enum):
    ANONYMOUS = 0
    EMAIL_VERIFIED = 1
    MFA_PASSED = 2
    ADMIN_VERIFIED = 3

@dataclass
class Session:
    session_id: str
    user_id: str | None
    trust_level: SessionTrust
    verified_factors: list[str] = field(default_factory=list)
    turn_count: int = 0

TRUST_INSTRUCTIONS = {
    SessionTrust.ANONYMOUS: (
        "You are talking to an unauthenticated visitor. "
        "You can only answer general product questions. "
        "Do NOT discuss account details, pricing, or internal data. "
        "Prompt the user to sign in for more assistance."
    ),
    SessionTrust.EMAIL_VERIFIED: (
        "User has verified their email. "
        "You can discuss their own account details and subscription information. "
        "Do NOT reveal other users' data or perform privileged operations."
    ),
    SessionTrust.MFA_PASSED: (
        "User has passed multi-factor authentication. "
        "You can assist with account modifications, billing changes, and data exports. "
        "Confirm before any irreversible actions."
    ),
    SessionTrust.ADMIN_VERIFIED: (
        "ADMIN SESSION VERIFIED. Full platform access authorized. "
        "You can manage user accounts, view system logs, and perform administrative actions. "
        "Log all admin actions in your response."
    ),
}

def build_session_prompt(session: Session) -> str:
    trust_block = TRUST_INSTRUCTIONS[session.trust_level]
    factors = f"Verified factors: {', '.join(session.verified_factors)}" if session.verified_factors else "No factors verified"
    return (
        f"Enterprise Assistant — Session {session.session_id}\n"
        f"Trust Level: {session.trust_level.name}\n"
        f"{factors}\n\n"
        f"{trust_block}"
    )

def handle_turn(session: Session, user_message: str) -> str:
    session.turn_count += 1
    system = build_session_prompt(session)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return resp.content[0].text

# Simulate escalating trust
sessions = [
    Session("sess_anon", None, SessionTrust.ANONYMOUS),
    Session("sess_email", "user_42", SessionTrust.EMAIL_VERIFIED, ["email"]),
    Session("sess_mfa", "user_42", SessionTrust.MFA_PASSED, ["email", "totp"]),
    Session("sess_admin", "admin_01", SessionTrust.ADMIN_VERIFIED, ["email", "totp", "admin_pin"]),
]

question = "Show me my account billing details and export my data."
for session in sessions:
    reply = handle_turn(session, question)
    print(f"[{session.trust_level.name}] {reply[:100]}...\n")

# Expected Token Savings: anonymous sessions use minimal prompts; admin sessions add detailed logging
# Environment: consumer apps with progressive auth, zero-trust enterprise portals
```

### Option 5: Department-Specific Knowledge Scope Injection

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

DEPARTMENT_KNOWLEDGE_SCOPES = {
    "engineering": {
        "allowed_topics": ["system architecture", "APIs", "debugging", "deployments", "databases"],
        "restricted_topics": ["sales forecasts", "customer PII", "financial projections"],
        "tone": "technical and precise",
        "tools_hint": "You can discuss code, infrastructure, and technical documentation."
    },
    "sales": {
        "allowed_topics": ["product features", "pricing", "customer success stories", "demos"],
        "restricted_topics": ["internal bugs", "unreleased features", "competitor analysis data"],
        "tone": "business-friendly and persuasive",
        "tools_hint": "You can help draft proposals, emails, and customer presentations."
    },
    "legal": {
        "allowed_topics": ["contracts", "compliance", "regulatory requirements", "IP"],
        "restricted_topics": ["specific legal advice", "client-confidential matters"],
        "tone": "precise and cautious",
        "tools_hint": "Always note that responses are for informational purposes only, not legal advice."
    },
    "hr": {
        "allowed_topics": ["policies", "benefits", "onboarding", "performance processes"],
        "restricted_topics": ["specific salary data", "individual performance ratings", "disciplinary records"],
        "tone": "empathetic and supportive",
        "tools_hint": "Direct sensitive individual matters to the appropriate HR business partner."
    }
}

@dataclass
class Employee:
    name: str
    department: str
    seniority: str  # junior / senior / director

def build_dept_prompt(employee: Employee) -> str:
    scope = DEPARTMENT_KNOWLEDGE_SCOPES.get(employee.department, DEPARTMENT_KNOWLEDGE_SCOPES["engineering"])
    allowed = ", ".join(scope["allowed_topics"])
    restricted = ", ".join(scope["restricted_topics"])
    seniority_note = ""
    if employee.seniority == "director":
        seniority_note = " As a Director, you may discuss cross-departmental strategic topics."

    return (
        f"You are an internal assistant for {employee.department.upper()} department.\n"
        f"User: {employee.name} (Seniority: {employee.seniority})\n"
        f"Tone: {scope['tone']}\n"
        f"Allowed topics: {allowed}\n"
        f"Do NOT discuss: {restricted}\n"
        f"{scope['tools_hint']}{seniority_note}"
    )

def dept_ask(employee: Employee, question: str) -> str:
    system = build_dept_prompt(employee)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

employees = [
    Employee("Alice", "engineering", "senior"),
    Employee("Bob", "sales", "junior"),
    Employee("Carol", "legal", "director"),
]

q = "Can you explain the company's data retention policy and any technical implementation details?"
for emp in employees:
    reply = dept_ask(emp, q)
    print(f"[{emp.department.upper()}/{emp.seniority}] {emp.name}: {reply[:100]}...\n")

# Expected Token Savings: scope injection is ~50 tokens; prevents costly out-of-scope responses
# Environment: enterprise knowledge assistants, internal wikis, department-scoped chat
```

### Option 6: Runtime Permission Update via Conversation Hook

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class LivePermissionContext:
    user_id: str
    base_role: str
    active_permissions: set[str] = field(default_factory=set)
    temporary_grants: dict[str, str] = field(default_factory=dict)  # permission -> reason
    revocations: set[str] = field(default_factory=set)

    def grant(self, permission: str, reason: str) -> None:
        self.temporary_grants[permission] = reason
        self.active_permissions.add(permission)
        print(f"  [GRANT] {permission}: {reason}")

    def revoke(self, permission: str) -> None:
        self.revocations.add(permission)
        self.active_permissions.discard(permission)
        print(f"  [REVOKE] {permission}")

    def build_permission_block(self) -> str:
        lines = [f"Base role: {self.base_role}"]
        if self.active_permissions:
            lines.append(f"Active permissions: {', '.join(sorted(self.active_permissions))}")
        if self.temporary_grants:
            for perm, reason in self.temporary_grants.items():
                lines.append(f"  GRANTED '{perm}' because: {reason}")
        if self.revocations:
            lines.append(f"REVOKED permissions: {', '.join(sorted(self.revocations))}")
        return "\n".join(lines)

def handle_with_live_permissions(ctx: LivePermissionContext,
                                  user_message: str,
                                  history: list[dict]) -> str:
    system = (
        "You are a permission-aware enterprise assistant.\n"
        + ctx.build_permission_block()
        + "\nStrictly enforce active permissions. Deny requests outside granted scope."
    )
    history_with_msg = history + [{"role": "user", "content": user_message}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=history_with_msg
    )
    return resp.content[0].text

# Start with standard permissions
ctx = LivePermissionContext(user_id="u_007", base_role="standard")
ctx.active_permissions = {"read:own_data", "view:public_docs"}
history = []

# Turn 1: basic question
reply = handle_with_live_permissions(ctx, "Show my account balance.", history)
print(f"Turn 1: {reply[:80]}...")
history += [{"role": "user", "content": "Show my account balance."}, {"role": "assistant", "content": reply}]

# Runtime permission grant (e.g., after supervisor approval)
ctx.grant("read:billing_history", "supervisor u_admin approved at 14:32")

# Turn 2: now has billing permission
reply = handle_with_live_permissions(ctx, "Show my last 3 invoices.", history)
print(f"Turn 2: {reply[:80]}...")
history += [{"role": "user", "content": "Show my last 3 invoices."}, {"role": "assistant", "content": reply}]

# Revoke after session
ctx.revoke("read:billing_history")
reply = handle_with_live_permissions(ctx, "Show last year's invoices.", history)
print(f"Turn 3 (after revoke): {reply[:80]}...")

# Expected Token Savings: runtime updates avoid session restart costs; permission block is ~30 tokens
# Environment: approval workflows, dynamic escalation, supervisor-grant flows
```

## Comparison

| Option | Injection Method | Dynamic Updates | Tool Scoping | Best For |
|--------|-----------------|-----------------|--------------|----------|
| 1 | Role enum → prompt block | No | No | Simple RBAC |
| 2 | Role → tool whitelist | No | Yes | Tool-use permission control |
| 3 | JWT claims → instructions | No | No | API-authenticated agents |
| 4 | Session trust level | No | No | Progressive auth unlock |
| 5 | Department knowledge scope | No | No | Enterprise department bots |
| 6 | Runtime permission grants | Yes | No | Approval workflow agents |
