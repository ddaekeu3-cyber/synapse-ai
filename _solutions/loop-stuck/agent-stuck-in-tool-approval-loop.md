---
layout: solution
title: "Agent stuck in tool approval loop"
category: loop-stuck
description: "Agent repeatedly requests permission or confirmation for the same tool action it has already been approved to perform, blocking indefinitely without executing."
tags: [loop-stuck, tool-failure, permissions, agentic, confirmation]
---

## Symptom

The agent asks for user confirmation before executing a tool, receives approval, but then asks again for the same permission on the next turn. The approval is never recorded or propagated, so the loop continues until the session times out. In automated pipelines, no human is present to approve and the agent simply stalls.

```
Turn 1: "May I run the database migration? (yes/no)"
Turn 2: User: "yes"
Turn 3: "Before I proceed, can I confirm you want me to run the migration? (yes/no)"
Turn 4: User: "YES, I already said yes"
Turn 5: "To confirm: should I execute the migration script? (yes/no)"
...
```

## Root Cause

Three common causes:
1. **Approval not persisted**: the agent stores approval in a local variable that is cleared between turns.
2. **System prompt re-triggers caution**: the model re-reads its safety instructions and re-asks from scratch.
3. **No approval state in context**: the conversation history only shows the user's "yes" as a message but no authoritative record that approval was granted for a specific action.

## Fix

Record approvals as structured state in the conversation context, use a dedicated approval tool to make grants machine-readable, or configure the agent to not ask for confirmation for pre-authorized action classes.

---

### Option 1 — Approval registry: structured approval state in context

```python
import anthropic
import json

client = anthropic.Anthropic()

class ApprovalRegistry:
    """Records granted approvals so the agent never re-asks for the same action."""

    def __init__(self) -> None:
        self._approvals: dict[str, bool] = {}

    def grant(self, action_id: str) -> None:
        self._approvals[action_id] = True
        print(f"[APPROVAL] Granted: '{action_id}'")

    def is_approved(self, action_id: str) -> bool:
        return self._approvals.get(action_id, False)

    def as_context_block(self) -> str:
        if not self._approvals:
            return ""
        granted = [k for k, v in self._approvals.items() if v]
        return f"\n[APPROVED ACTIONS — do NOT re-ask for these]: {granted}\n"

registry = ApprovalRegistry()

SYSTEM_BASE = """
You are an operations assistant. For destructive or irreversible actions,
ask for approval ONCE using the request_approval tool.
If an action is listed in [APPROVED ACTIONS], proceed immediately — do NOT ask again.
"""

TOOLS = [
    {
        "name": "request_approval",
        "description": "Ask the user to approve a specific action. Use only if the action is NOT already in APPROVED ACTIONS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id":   {"type": "string", "description": "Unique identifier for this action, e.g. 'run_db_migration_v5'"},
                "description": {"type": "string", "description": "Human-readable description of what will happen"},
            },
            "required": ["action_id", "description"],
        },
    },
    {
        "name": "run_migration",
        "description": "Execute the database migration script.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

def simulate_user_response(prompt: str) -> str:
    """Simulate the user approving actions."""
    print(f"[USER PROMPT]: {prompt[:80]}")
    return "yes, approved"

def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        system = SYSTEM_BASE + registry.as_context_block()

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue

            if b.name == "request_approval":
                action_id   = b.input.get("action_id", "unknown")
                description = b.input.get("description", "")

                if registry.is_approved(action_id):
                    # Agent incorrectly re-asked — short-circuit
                    content = json.dumps({"status": "already_approved", "action_id": action_id,
                                         "message": "This action was already approved. Proceed immediately."})
                else:
                    user_reply = simulate_user_response(description)
                    if "yes" in user_reply.lower():
                        registry.grant(action_id)
                        content = json.dumps({"status": "approved", "action_id": action_id})
                    else:
                        content = json.dumps({"status": "denied", "action_id": action_id})

            elif b.name == "run_migration":
                action_id = "run_db_migration"
                if registry.is_approved(action_id):
                    content = json.dumps({"status": "success", "message": "Migration executed successfully."})
                else:
                    content = json.dumps({"status": "blocked", "message": "Approval required before running migration."})

            else:
                content = json.dumps({"error": "unknown tool"})

            results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})

        messages.append({"role": "user", "content": results})

    return next(b.text for b in response.content if hasattr(b, "text"))

result = agent_loop("Please run the database migration to v5. Get my approval first.")
print(f"\nFinal: {result}")
```

**Expected Token Savings:** Eliminates the approval loop entirely; the registry ensures at most one approval request per action regardless of how many times the model's caution reflex re-triggers.

**Environment:** Any agentic pipeline requiring destructive action confirmation; the registry is in-memory per session but can be persisted for multi-session workflows.

---

### Option 2 — Pre-authorization list: skip confirmation for known-safe actions

```python
import anthropic
import json

client = anthropic.Anthropic()

# Actions pre-authorized by the operator — agent never asks for these
PRE_AUTHORIZED = {
    "read_file",
    "list_directory",
    "search_logs",
    "check_status",
    "run_linter",
}

# Actions that always require fresh user approval
ALWAYS_CONFIRM = {
    "delete_data",
    "deploy_production",
    "send_email",
    "charge_customer",
}

def authorization_policy(tool_name: str) -> str:
    if tool_name in PRE_AUTHORIZED:
        return "pre_authorized"
    if tool_name in ALWAYS_CONFIRM:
        return "requires_approval"
    return "requires_approval"   # default: require approval for unknown tools

SYSTEM = f"""
You are a developer assistant. Authorization policy:
- Pre-authorized (no confirmation needed): {sorted(PRE_AUTHORIZED)}
- Always confirm: {sorted(ALWAYS_CONFIRM)}
- For pre-authorized tools, proceed immediately without asking.
- For confirmation-required tools, ask ONCE using request_approval.
"""

TOOLS = [
    {"name": "read_file",      "description": "Read a file.",     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_directory", "description": "List directory.",   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "delete_data",    "description": "Delete records.",   "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "request_approval","description": "Request user approval for a sensitive action.",
     "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "reason": {"type": "string"}}, "required": ["action", "reason"]}},
]

_session_approvals: set[str] = set()

def execute_tool(name: str, inputs: dict) -> str:
    policy = authorization_policy(name)

    if policy == "pre_authorized":
        print(f"[PRE-AUTH] {name}({inputs}) — executing without confirmation")
        return json.dumps({"status": "ok", "result": f"Executed {name} on {inputs}"})

    if name == "request_approval":
        action = inputs.get("action", "")
        if action in _session_approvals:
            return json.dumps({"status": "already_approved", "proceed": True})
        # Simulate user saying yes
        print(f"[USER] Approving: {inputs.get('reason', action)}")
        _session_approvals.add(action)
        return json.dumps({"status": "approved", "proceed": True})

    if name == "delete_data":
        if "delete_data" in _session_approvals or name in _session_approvals:
            return json.dumps({"status": "deleted", "table": inputs.get("table")})
        return json.dumps({"status": "blocked", "message": "Request approval first."})

    return json.dumps({"error": "unknown"})

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": execute_tool(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run(
    "List the /tmp directory, then delete the 'temp_logs' table. "
    "You don't need to confirm the listing."
))
```

**Expected Token Savings:** Pre-authorization eliminates confirmation round-trips for safe actions; the policy is defined once in the system prompt rather than repeated in every agent loop.

**Environment:** DevOps and automation agents; maintain `PRE_AUTHORIZED` in a config file updated by the ops team rather than hard-coding.

---

### Option 3 — One-shot approval tool with granted permission token

```python
import anthropic
import json
import secrets
import time

client = anthropic.Anthropic()

# Permission token store
_tokens: dict[str, dict] = {}

def create_permission_token(action: str, ttl: float = 300.0) -> str:
    token = secrets.token_hex(8)
    _tokens[token] = {"action": action, "granted_at": time.monotonic(), "ttl": ttl}
    return token

def verify_token(token: str, action: str) -> bool:
    entry = _tokens.get(token)
    if not entry:
        return False
    if entry["action"] != action:
        return False
    if time.monotonic() - entry["granted_at"] > entry["ttl"]:
        del _tokens[token]
        return False
    return True

TOOLS = [
    {
        "name": "request_permission",
        "description": (
            "Request a permission token for a sensitive action. Returns a token if approved. "
            "Pass the token to execute_sensitive_action. Do NOT call request_permission again "
            "if you already have a token for this action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "Action identifier"},
                "description": {"type": "string", "description": "What will happen"},
            },
            "required": ["action", "description"],
        },
    },
    {
        "name": "execute_sensitive_action",
        "description": "Execute a sensitive action using a permission token.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Action identifier"},
                "token":  {"type": "string", "description": "Permission token from request_permission"},
            },
            "required": ["action", "token"],
        },
    },
]

SYSTEM = (
    "You are an ops assistant. For sensitive actions, call request_permission ONCE "
    "to get a token, then pass the token to execute_sensitive_action. "
    "Never call request_permission more than once for the same action."
)

def handle_tool(name: str, inputs: dict) -> str:
    if name == "request_permission":
        action = inputs.get("action", "")
        desc   = inputs.get("description", "")
        print(f"[USER APPROVAL] '{desc}' → granted")
        token = create_permission_token(action)
        return json.dumps({"approved": True, "token": token,
                           "instruction": f"Use this token with execute_sensitive_action for action='{action}'"})

    if name == "execute_sensitive_action":
        action = inputs.get("action", "")
        token  = inputs.get("token", "")
        if verify_token(token, action):
            return json.dumps({"status": "success", "action": action,
                               "message": f"Action '{action}' executed successfully."})
        return json.dumps({"status": "error", "message": "Invalid or expired token. Request permission again."})

    return json.dumps({"error": "unknown tool"})

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": handle_tool(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Send a deployment notification email to the ops team. Get my permission first."))
```

**Expected Token Savings:** Token-based permission makes the grant machine-readable in the conversation context; the model cannot re-enter the approval loop because the token proves prior approval; tokens expire after TTL preventing stale grants.

**Environment:** Multi-step automation pipelines where a single user session grants permission for a sequence of actions; token TTL prevents permission from persisting across unrelated sessions.

---

### Option 4 — Automated pipeline mode: suppress all confirmation requests

```python
import anthropic
import json

client = anthropic.Anthropic()

# Pipeline mode: no human in the loop, all approved actions pre-declared
PIPELINE_CONFIG = {
    "mode": "automated",
    "approved_actions": ["run_tests", "build_image", "push_to_staging", "notify_slack"],
    "forbidden_actions": ["deploy_production", "delete_database", "send_external_email"],
    "on_forbidden": "fail_fast",   # raise error instead of asking user
}

SYSTEM = f"""
You are an automated CI/CD pipeline agent. Configuration:
- Mode: {PIPELINE_CONFIG['mode']} (no human in the loop)
- Pre-approved actions: {PIPELINE_CONFIG['approved_actions']}
- Forbidden actions: {PIPELINE_CONFIG['forbidden_actions']}

Rules:
1. Execute pre-approved actions immediately — no confirmation required.
2. For forbidden actions, report an error and stop.
3. NEVER ask the user for confirmation — this is a fully automated pipeline.
4. Do NOT call request_approval — it is not available in automated mode.
""".strip()

TOOLS = [
    {
        "name": tool_name,
        "description": f"Execute pipeline step: {tool_name}.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    for tool_name in PIPELINE_CONFIG["approved_actions"] + PIPELINE_CONFIG["forbidden_actions"]
]

def execute_pipeline_tool(name: str) -> str:
    if name in PIPELINE_CONFIG["forbidden_actions"]:
        return json.dumps({
            "status": "FORBIDDEN",
            "action": name,
            "message": f"Action '{name}' is not permitted in automated mode. Pipeline halted.",
        })
    if name in PIPELINE_CONFIG["approved_actions"]:
        print(f"[PIPELINE] Executing: {name}")
        return json.dumps({"status": "success", "action": name, "duration_ms": 1200})
    return json.dumps({"status": "error", "message": "Unknown action"})

def run_pipeline(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": execute_pipeline_tool(b.name)}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run_pipeline(
    "Run the test suite, build the Docker image, push to staging, and notify Slack."
))
```

**Expected Token Savings:** Automated mode eliminates all confirmation overhead; pipeline completes in the minimum number of turns without any approval round-trips; forbidden action fast-fail prevents wasted turns on unpermitted work.

**Environment:** CI/CD pipelines, cron-triggered agents, batch processors where no human is present; configure `approved_actions` per pipeline role (e.g., staging vs. production deployer).

---

### Option 5 — Conversation-scoped approval store injected into every turn

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

class SessionApprovals:
    """Injects a compact approval record into the system prompt every turn."""

    def __init__(self) -> None:
        self._log: list[dict] = []

    def record(self, action: str, approved: bool, rationale: str = "") -> None:
        self._log.append({
            "action": action,
            "approved": approved,
            "rationale": rationale,
            "ts": time.strftime("%H:%M:%S"),
        })

    def system_block(self) -> str:
        if not self._log:
            return ""
        lines = ["\n## Approval Log (DO NOT re-ask for actions already decided)\n"]
        for entry in self._log:
            status = "APPROVED" if entry["approved"] else "DENIED"
            lines.append(f"- [{entry['ts']}] {status}: {entry['action']}"
                         + (f" — {entry['rationale']}" if entry["rationale"] else ""))
        lines.append("\nFor APPROVED actions, proceed without asking again.")
        return "\n".join(lines)

    def is_approved(self, action: str) -> bool | None:
        for entry in reversed(self._log):
            if entry["action"] == action:
                return entry["approved"]
        return None

approvals = SessionApprovals()

BASE_SYSTEM = (
    "You are a database maintenance agent. Ask for user approval before any "
    "destructive operation. Once approved, do NOT ask again — proceed immediately."
)

TOOLS = [
    {"name": "confirm_action",  "description": "Ask user to confirm a destructive action.",
     "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "details": {"type": "string"}}, "required": ["action"]}},
    {"name": "vacuum_table",    "description": "VACUUM a database table.", "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "reindex_table",   "description": "REINDEX a database table.", "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "truncate_table",  "description": "TRUNCATE a table (destructive).", "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
]

def handle(name: str, inputs: dict) -> str:
    if name == "confirm_action":
        action = inputs.get("action", "")
        existing = approvals.is_approved(action)
        if existing is True:
            return json.dumps({"status": "already_approved", "message": f"'{action}' was already approved."})
        print(f"[USER] Approving: {inputs.get('details', action)}")
        approvals.record(action, approved=True, rationale="user confirmed")
        return json.dumps({"status": "approved", "action": action})

    for tool_name in ["vacuum_table", "reindex_table", "truncate_table"]:
        if name == tool_name:
            print(f"[DB] Executing {tool_name}({inputs})")
            return json.dumps({"status": "success", "operation": tool_name, "table": inputs.get("table")})

    return json.dumps({"error": "unknown"})

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        system = BASE_SYSTEM + approvals.system_block()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": handle(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run(
    "VACUUM the orders table, REINDEX the users table, and TRUNCATE the temp_cache table. "
    "Confirm with me before each destructive operation."
))
```

**Expected Token Savings:** Approval log in system prompt prevents re-asks; small overhead (~50 tokens per approved action) eliminates full approval round-trips (~300–500 tokens each).

**Environment:** Interactive agents performing multi-step destructive operations in sequence; the approval log also serves as an audit trail.

---

### Option 6 — Max-confirmation guard: hard limit on re-asks per action

```python
import anthropic
import json
from collections import defaultdict

client = anthropic.Anthropic()

MAX_ASKS_PER_ACTION = 1   # hard limit: never ask more than once per action per session

class ConfirmationGuard:
    def __init__(self, max_asks: int = MAX_ASKS_PER_ACTION) -> None:
        self._ask_counts: dict[str, int] = defaultdict(int)
        self._approvals:  dict[str, bool] = {}
        self._max_asks = max_asks

    def can_ask(self, action: str) -> bool:
        return self._ask_counts[action] < self._max_asks and action not in self._approvals

    def record_ask(self, action: str) -> None:
        self._ask_counts[action] += 1

    def record_decision(self, action: str, approved: bool) -> None:
        self._approvals[action] = approved

    def is_decided(self, action: str) -> bool:
        return action in self._approvals

    def is_approved(self, action: str) -> bool:
        return self._approvals.get(action, False)

    def intercept_tool_result(self, tool_name: str, action_id: str, proposed_result: str) -> str:
        """
        Guard: if the model is trying to ask again for an already-decided action,
        override the tool result to short-circuit the loop.
        """
        if tool_name == "confirm_action" and self.is_decided(action_id):
            status = "approved" if self.is_approved(action_id) else "denied"
            return json.dumps({
                "status": status,
                "message": f"Already decided: {status}. Do not ask again.",
                "__loop_guard": True,
            })
        return proposed_result

guard = ConfirmationGuard(max_asks=1)

TOOLS = [
    {"name": "confirm_action", "description": "Confirm a sensitive action with the user.",
     "input_schema": {"type": "object", "properties": {"action_id": {"type": "string"}, "what_will_happen": {"type": "string"}}, "required": ["action_id"]}},
    {"name": "execute_action", "description": "Execute the action after confirmation.",
     "input_schema": {"type": "object", "properties": {"action_id": {"type": "string"}}, "required": ["action_id"]}},
]

SYSTEM = (
    "You are an assistant that requires user confirmation for sensitive actions. "
    "Ask for confirmation ONCE per action, then proceed based on the answer."
)

def handle_tool(name: str, inputs: dict) -> str:
    if name == "confirm_action":
        action_id = inputs.get("action_id", "")

        # Guard check before executing
        if guard.is_decided(action_id):
            return guard.intercept_tool_result(name, action_id, "")

        if not guard.can_ask(action_id):
            return json.dumps({"status": "error", "message": f"Max ask limit reached for '{action_id}'"})

        guard.record_ask(action_id)
        # Simulate approval
        print(f"[USER] Approving '{action_id}': {inputs.get('what_will_happen', '')}")
        guard.record_decision(action_id, approved=True)
        return json.dumps({"status": "approved", "action_id": action_id})

    if name == "execute_action":
        action_id = inputs.get("action_id", "")
        if guard.is_approved(action_id):
            return json.dumps({"status": "success", "executed": action_id})
        return json.dumps({"status": "blocked", "message": "Not approved."})

    return json.dumps({"error": "unknown"})

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": handle_tool(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Please purge the old_sessions table. Ask me to confirm first."))
```

**Expected Token Savings:** Hard ask limit is a safety net that prevents runaway approval loops even if all other mechanisms fail; the guard intercepts and overrides redundant confirmation calls at the orchestration layer without relying on model behavior.

**Environment:** Defense-in-depth layer for any agent with confirmation requirements; pair with Option 1 or 5 as the primary mechanism.

---

## Comparison

| Option | Loop Prevention | Human Required | Persists Across Turns | Best For |
|--------|---------------|---------------|----------------------|---------|
| 1 — Approval registry | Structured state | Yes | Yes (registry) | Interactive agents |
| 2 — Pre-authorization | Policy declaration | No | N/A | Known-safe action classes |
| 3 — Permission token | Cryptographic token | Yes | Yes (token) | Multi-step sensitive ops |
| 4 — Automated pipeline | Mode flag | No | N/A | Unattended CI/CD |
| 5 — Approval log in prompt | System prompt injection | Yes | Yes (prompt) | Multi-action sessions |
| 6 — Max-ask guard | Hard count limit | Yes | Yes (guard) | Defense-in-depth |

**Recommended default:** Option 1 (approval registry) for interactive agents — simple, reliable, and auditable. Add Option 6 (max-ask guard) as a safety backstop to catch loops that slip through the primary mechanism.
