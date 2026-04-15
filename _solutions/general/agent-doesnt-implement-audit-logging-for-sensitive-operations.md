---
layout: solution
title: "Agent Doesn't Implement Audit Logging for Sensitive Operations"
category: general
description: "Agent performs sensitive operations — deleting records, sending emails, modifying permissions, making financial transactions — without logging who requested what, when, and what the outcome was, making post-incident investigation impossible."
tags: [general, audit-logging, compliance, security, observability, accountability]
---

## Symptom

An agent accidentally deletes 500 customer records. The engineering team investigates, but there are no logs of which user triggered the operation, what parameters were passed, whether a confirmation was requested, or what the agent's intermediate reasoning was. The incident report cannot answer "who, what, when, why" — the four questions every compliance audit requires. In another case, an agent sends 10,000 marketing emails to users who opted out, but there is no record of which tool call triggered the send or what the agent's reasoning was.

## Root Cause

Developers focus on the agent's happy-path functionality and treat logging as a secondary concern. Standard application logging captures exceptions and errors but not the agent's decision-making context: which user asked what, what the agent planned to do, what tools were called with what parameters, and what the results were. For sensitive operations, this observability gap makes it impossible to audit agent behavior, detect abuse, or reconstruct what happened after an incident.

## Fix

### Option 1 — Structured audit log for every tool call

```python
import json
import time
import uuid
import logging
import anthropic

client = anthropic.Anthropic()

# Configure structured JSON logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
audit_logger = logging.getLogger("audit")

SENSITIVE_TOOLS = {"delete_record", "send_email", "modify_permissions", "process_payment", "export_data"}

def audit_log(event: str, **fields) -> None:
    """Emit a structured audit log entry."""
    entry = {
        "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event":      event,
        **fields,
    }
    audit_logger.info(json.dumps(entry))

TOOLS = [
    {
        "name": "delete_record",
        "description": "Delete a record from the database by ID.",
        "input_schema": {
            "type": "object",
            "required": ["table", "record_id"],
            "properties": {
                "table":     {"type": "string"},
                "record_id": {"type": "string"},
            },
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to a user.",
        "input_schema": {
            "type": "object",
            "required": ["to", "subject", "body"],
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
        },
    },
    {
        "name": "query_database",
        "description": "Run a read-only SQL query.",
        "input_schema": {
            "type": "object",
            "required": ["sql"],
            "properties": {"sql": {"type": "string"}},
        },
    },
]

def execute_tool(name: str, inputs: dict, request_id: str) -> str:
    """Execute a tool with full audit logging."""
    is_sensitive = name in SENSITIVE_TOOLS

    audit_log(
        "tool_call_started",
        request_id=request_id,
        tool=name,
        inputs=inputs,
        sensitive=is_sensitive,
    )

    t0 = time.monotonic()
    try:
        # Simulate tool execution
        if name == "delete_record":
            result = {"deleted": True, "table": inputs["table"], "id": inputs["record_id"]}
        elif name == "send_email":
            result = {"sent": True, "to": inputs["to"], "message_id": f"msg_{uuid.uuid4().hex[:8]}"}
        elif name == "query_database":
            result = {"rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "count": 2}
        else:
            result = {"error": "unknown tool"}

        latency_ms = round((time.monotonic() - t0) * 1000)
        audit_log(
            "tool_call_completed",
            request_id=request_id,
            tool=name,
            result=result,
            latency_ms=latency_ms,
            sensitive=is_sensitive,
        )
        return json.dumps(result)

    except Exception as e:
        audit_log(
            "tool_call_failed",
            request_id=request_id,
            tool=name,
            error=str(e),
            sensitive=is_sensitive,
        )
        raise

def run_agent(user_request: str, user_id: str = "user_123") -> str:
    request_id = uuid.uuid4().hex[:12]
    audit_log("request_started", request_id=request_id, user_id=user_id, request=user_request)

    messages = [{"role": "user", "content": user_request}]
    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            final = next((b.text for b in r.content if b.type == "text"), "")
            audit_log("request_completed", request_id=request_id, user_id=user_id,
                      tokens=r.usage.input_tokens + r.usage.output_tokens)
            return final
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = execute_tool(b.name, b.input, request_id)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

r = run_agent("Query the database for users, then delete user with id 42.", user_id="admin_7")
print(f"\nAgent response: {r[:200]}")
```

**Expected Token Savings:** Audit logging adds zero tokens to LLM calls; it is pure infrastructure — but without it, a post-incident investigation costs 10-100× more engineering time than adding the logging upfront.
**Environment:** All agents performing mutations or accessing sensitive data; structured audit logging with request IDs is the minimum viable observability requirement for any production agent.

---

### Option 2 — Pre-execution audit hook with human-readable trail

```python
import json
import time
import uuid
import anthropic

client = anthropic.Anthropic()

class AuditTrail:
    """Accumulates an audit trail for a single agent session."""

    def __init__(self, session_id: str, user_id: str) -> None:
        self.session_id = session_id
        self.user_id    = user_id
        self._events:   list[dict] = []
        self._start     = time.time()

    def record(self, event_type: str, **data) -> None:
        self._events.append({
            "seq":        len(self._events) + 1,
            "type":       event_type,
            "elapsed_s":  round(time.time() - self._start, 2),
            **data,
        })

    def sensitive_events(self) -> list[dict]:
        SENSITIVE = {"delete", "send", "payment", "permission", "export"}
        return [
            e for e in self._events
            if any(s in e.get("tool", "").lower() for s in SENSITIVE)
        ]

    def to_report(self) -> str:
        lines = [
            f"=== Audit Trail ===",
            f"Session: {self.session_id}",
            f"User:    {self.user_id}",
            f"Duration: {time.time() - self._start:.1f}s",
            f"Events: {len(self._events)}",
            f"Sensitive operations: {len(self.sensitive_events())}",
            "",
        ]
        for e in self._events:
            lines.append(f"[{e['seq']:02d} +{e['elapsed_s']:.2f}s] {e['type'].upper()}")
            for k, v in e.items():
                if k not in ("seq", "type", "elapsed_s"):
                    lines.append(f"     {k}: {json.dumps(v)[:80]}")
        return "\n".join(lines)

TOOLS = [
    {"name": "send_notification", "description": "Send a push notification.", "input_schema": {"type": "object", "required": ["user_id", "message"], "properties": {"user_id": {"type": "string"}, "message": {"type": "string"}}}},
    {"name": "update_plan",       "description": "Update a user's subscription plan.", "input_schema": {"type": "object", "required": ["user_id", "plan"], "properties": {"user_id": {"type": "string"}, "plan": {"type": "string", "enum": ["free", "pro", "enterprise"]}}}},
]

def run_audited_agent(request: str, user_id: str) -> tuple[str, AuditTrail]:
    trail = AuditTrail(session_id=uuid.uuid4().hex[:8], user_id=user_id)
    trail.record("request", content=request)

    messages = [{"role": "user", "content": request}]
    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            answer = next((b.text for b in r.content if b.type == "text"), "")
            trail.record("response", content=answer[:200])
            return answer, trail
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                trail.record("tool_call", tool=b.name, inputs=b.input)
                sim_result = {"ok": True, "tool": b.name}
                trail.record("tool_result", tool=b.name, result=sim_result)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(sim_result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached", trail

answer, trail = run_audited_agent(
    "Upgrade user_456 to the pro plan and send them a welcome notification.",
    user_id="admin_1",
)
print(trail.to_report())
print(f"\nAnswer: {answer[:200]}")
```

**Expected Token Savings:** Human-readable audit trails add no LLM cost; they compress the 3-hour investigation of a post-incident into a 3-minute report review — especially valuable when the sensitive_events() filter highlights exactly which operations to examine.
**Environment:** Agents with administrative privileges; the AuditTrail class can be serialised to S3/database for long-term retention and compliance archiving.

---

### Option 3 — Operation classifier: auto-tag each tool call by sensitivity level

```python
import json
import time
import anthropic

client = anthropic.Anthropic()

# Sensitivity levels determine audit depth
SENSITIVITY_LEVELS = {
    "LOW":      ["query_database", "read_file", "list_users", "get_config"],
    "MEDIUM":   ["update_record", "create_record", "send_notification", "write_file"],
    "HIGH":     ["delete_record", "bulk_delete", "export_data", "modify_permissions"],
    "CRITICAL": ["send_email_blast", "process_payment", "drop_table", "revoke_access"],
}

def get_sensitivity(tool_name: str) -> str:
    for level, tools in SENSITIVITY_LEVELS.items():
        if tool_name in tools:
            return level
    return "MEDIUM"   # default to medium for unknown tools

AUDIT_ACTIONS = {
    "LOW":      lambda name, inputs, result: print(f"  [LOG] {name}"),
    "MEDIUM":   lambda name, inputs, result: print(f"  [AUDIT] {name} inputs={json.dumps(inputs)[:80]}"),
    "HIGH":     lambda name, inputs, result: print(f"  [HIGH AUDIT] {name} inputs={inputs} result={result}"),
    "CRITICAL": lambda name, inputs, result: (
        print(f"  [CRITICAL AUDIT] {name}"),
        print(f"    inputs={json.dumps(inputs)}"),
        print(f"    result={json.dumps(result)}"),
        print(f"    timestamp={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"),
        # In production: also page on-call, write to immutable log, require 2FA confirmation
    ),
}

TOOLS = [
    {"name": "query_database",      "description": "Run a read query.",           "input_schema": {"type": "object", "required": ["sql"],    "properties": {"sql":    {"type": "string"}}}},
    {"name": "delete_record",       "description": "Delete a record.",             "input_schema": {"type": "object", "required": ["id"],     "properties": {"id":     {"type": "string"}}}},
    {"name": "process_payment",     "description": "Process a payment.",           "input_schema": {"type": "object", "required": ["amount"], "properties": {"amount": {"type": "number"}}}},
    {"name": "send_notification",   "description": "Send a notification.",         "input_schema": {"type": "object", "required": ["msg"],    "properties": {"msg":    {"type": "string"}}}},
]

def execute_with_sensitivity_audit(name: str, inputs: dict) -> str:
    level = get_sensitivity(name)
    result = {"ok": True, "tool": name, "sensitivity": level}
    AUDIT_ACTIONS[level](name, inputs, result)
    return json.dumps(result)

def run_agent(request: str) -> str:
    messages = [{"role": "user", "content": request}]
    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                res = execute_with_sensitivity_audit(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": res})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

requests = [
    "Query the database for users.",
    "Delete record with id user_789.",
    "Process a payment of $99.99.",
]
for req in requests:
    print(f"\nRequest: {req}")
    run_agent(req)
```

**Expected Token Savings:** Sensitivity classification adds zero LLM calls; it routes CRITICAL operations to full immutable logging and LOW operations to lightweight logs — preventing audit log bloat (logging everything at maximum detail) while ensuring critical operations are never missed.
**Environment:** Agents with mixed tool sensitivity levels; tiered sensitivity audit logging is the scalable approach when some tools are read-only and some are irreversible.

---

### Option 4 — Immutable append-only audit log with checksum chain

```python
import hashlib
import json
import time
import uuid
import anthropic

client = anthropic.Anthropic()

class ImmutableAuditLog:
    """
    Append-only log where each entry includes the hash of the previous entry.
    Tampering with any entry breaks the chain — detectable in verification.
    """

    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._prev_hash = "0" * 64   # genesis hash

    def append(self, event: str, **data) -> str:
        entry = {
            "id":        uuid.uuid4().hex,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event":     event,
            "prev_hash": self._prev_hash,
            **data,
        }
        entry_json  = json.dumps(entry, sort_keys=True)
        entry_hash  = hashlib.sha256(entry_json.encode()).hexdigest()
        entry["hash"] = entry_hash
        self._prev_hash = entry_hash
        self._entries.append(entry)
        return entry_hash

    def verify_integrity(self) -> bool:
        """Re-compute hash chain — returns False if any entry was tampered with."""
        prev = "0" * 64
        for entry in self._entries:
            check = {k: v for k, v in entry.items() if k != "hash"}
            check["prev_hash"] = prev
            expected = hashlib.sha256(json.dumps(check, sort_keys=True).encode()).hexdigest()
            if expected != entry["hash"]:
                print(f"  [INTEGRITY FAIL] entry {entry['id']} hash mismatch")
                return False
            prev = entry["hash"]
        return True

    def export(self) -> list[dict]:
        return list(self._entries)

_LOG = ImmutableAuditLog()

TOOLS = [
    {"name": "delete_user", "description": "Delete a user account.", "input_schema": {"type": "object", "required": ["user_id"], "properties": {"user_id": {"type": "string"}}}},
    {"name": "modify_role",  "description": "Change a user's role.",  "input_schema": {"type": "object", "required": ["user_id", "role"], "properties": {"user_id": {"type": "string"}, "role": {"type": "string"}}}},
]

def run_agent_with_immutable_log(request: str, actor_id: str) -> str:
    _LOG.append("session_start", actor_id=actor_id, request=request)
    messages = [{"role": "user", "content": request}]

    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            answer = next((b.text for b in r.content if b.type == "text"), "")
            _LOG.append("session_end", actor_id=actor_id, answer=answer[:100])
            return answer
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                h = _LOG.append("tool_called", actor_id=actor_id, tool=b.name, inputs=b.input)
                print(f"  [audit hash] {h[:16]}...")
                result = {"ok": True}
                _LOG.append("tool_result", actor_id=actor_id, tool=b.name, result=result)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

run_agent_with_immutable_log("Delete user account user_42.", actor_id="admin_1")
print(f"\nLog entries: {len(_LOG.export())}")
print(f"Chain integrity: {_LOG.verify_integrity()}")
print(f"\nLast entry: {json.dumps(_LOG.export()[-1], indent=2)[:300]}")
```

**Expected Token Savings:** Hash-chained audit logs add zero LLM cost; tamper-evident logging is a compliance requirement in SOC 2, GDPR, and PCI DSS — implementing it prevents regulatory fines that can dwarf all API token costs combined.
**Environment:** Agents performing financial, healthcare, or legally sensitive operations; immutable logs are required by compliance frameworks and provide non-repudiation evidence in disputes.

---

### Option 5 — Reasoning audit: capture the agent's decision rationale

```python
import json
import anthropic

client = anthropic.Anthropic()

REASONING_SYSTEM = """You are an agent that audits its own reasoning.

Before calling any sensitive tool (delete, send, modify, payment), you MUST first output a reasoning block:

<AUDIT_REASONING>
action: [the tool you're about to call]
inputs: [key parameters]
why: [why this action is needed based on the user's request]
reversible: [yes/no]
user_intent_confidence: [0-100]%
</AUDIT_REASONING>

Then proceed with the tool call. This reasoning block is logged for compliance."""

import re

def extract_audit_reasoning(text: str) -> list[dict]:
    """Parse AUDIT_REASONING blocks from agent response."""
    blocks = re.findall(r"<AUDIT_REASONING>(.*?)</AUDIT_REASONING>", text, re.DOTALL)
    parsed = []
    for block in blocks:
        entry = {}
        for line in block.strip().splitlines():
            if ": " in line:
                key, _, value = line.partition(": ")
                entry[key.strip()] = value.strip()
        if entry:
            parsed.append(entry)
    return parsed

TOOLS = [
    {"name": "delete_record",  "description": "Delete a record.", "input_schema": {"type": "object", "required": ["id"],    "properties": {"id":    {"type": "string"}}}},
    {"name": "send_email",     "description": "Send an email.",   "input_schema": {"type": "object", "required": ["to"],    "properties": {"to":    {"type": "string"}, "body": {"type": "string"}}}},
]

def run_reasoning_audited(request: str) -> dict:
    all_reasoning: list[dict] = []
    messages = [{"role": "user", "content": request}]

    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=REASONING_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        # Extract reasoning from any text blocks
        for block in r.content:
            if block.type == "text":
                reasoning = extract_audit_reasoning(block.text)
                if reasoning:
                    all_reasoning.extend(reasoning)
                    print(f"  [audit reasoning] {reasoning}")

        if r.stop_reason == "end_turn":
            return {"answer": next((b.text for b in r.content if b.type == "text"), ""),
                    "reasoning_log": all_reasoning}
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": '{"ok": true}'})
        messages.append({"role": "user", "content": results})
    return {"answer": "max steps", "reasoning_log": all_reasoning}

result = run_reasoning_audited("Delete the test account user_99 and send a confirmation email to admin@example.com.")
print(f"\nAnswer: {result['answer'][:150]}")
print(f"\nReasoning audit log:")
for r in result["reasoning_log"]:
    print(f"  {r}")
```

**Expected Token Savings:** Reasoning audit blocks add ~50 tokens per sensitive operation but capture the agent's intent — enabling investigators to answer "did the agent act appropriately?" even when the outcome was wrong; reasoning logs are uniquely valuable because they capture information that no post-hoc analysis can reconstruct.
**Environment:** Agents with autonomous decision authority over sensitive operations; reasoning capture is most valuable when the agent must justify its actions to humans after the fact.

---

### Option 6 — Compliance report generator: periodic audit summary from logs

```python
import json
import time
import collections
import anthropic

client = anthropic.Anthropic()

# Simulated accumulated audit log entries
SAMPLE_LOG = [
    {"timestamp": "2026-04-15T10:00:00Z", "event": "tool_called", "tool": "delete_record",   "actor": "admin_1", "inputs": {"id": "user_42"}},
    {"timestamp": "2026-04-15T10:01:00Z", "event": "tool_called", "tool": "send_email",       "actor": "agent",   "inputs": {"to": "user@example.com"}},
    {"timestamp": "2026-04-15T10:02:00Z", "event": "tool_called", "tool": "modify_permissions","actor": "admin_2", "inputs": {"user_id": "user_10", "role": "admin"}},
    {"timestamp": "2026-04-15T10:03:00Z", "event": "tool_called", "tool": "delete_record",   "actor": "admin_1", "inputs": {"id": "user_43"}},
    {"timestamp": "2026-04-15T10:04:00Z", "event": "tool_called", "tool": "query_database",  "actor": "agent",   "inputs": {"sql": "SELECT * FROM users"}},
    {"timestamp": "2026-04-15T10:05:00Z", "event": "tool_called", "tool": "export_data",     "actor": "admin_3", "inputs": {"table": "payments"}},
]

REPORT_SYSTEM = """You are a compliance audit analyst.
Given a structured audit log, produce a concise compliance summary covering:
1. Total operations by sensitivity tier
2. Any anomalous patterns (e.g., bulk deletes, unusual actors, off-hours operations)
3. Operations requiring manual review
4. Recommended follow-up actions

Be factual and specific. Reference actor IDs and timestamps."""

def generate_compliance_report(log_entries: list[dict]) -> str:
    # Compute statistics
    by_tool   = collections.Counter(e["tool"] for e in log_entries)
    by_actor  = collections.Counter(e["actor"] for e in log_entries)
    sensitive = [e for e in log_entries if e["tool"] in {"delete_record", "modify_permissions", "export_data"}]

    summary = {
        "total_events":        len(log_entries),
        "events_by_tool":      dict(by_tool),
        "events_by_actor":     dict(by_actor),
        "sensitive_operations": len(sensitive),
        "sensitive_details":   sensitive,
    }

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=REPORT_SYSTEM,
        messages=[{"role": "user", "content": f"Audit log summary:\n{json.dumps(summary, indent=2)}"}],
    )
    return r.content[0].text

report = generate_compliance_report(SAMPLE_LOG)
print("=== Compliance Report ===")
print(report)
```

**Expected Token Savings:** Compliance report generation costs ~400 tokens once per reporting period (daily/weekly) instead of requiring a human analyst to manually review thousands of raw log lines; automated reports make continuous compliance monitoring economically viable.
**Environment:** Agents in regulated industries (finance, healthcare, enterprise SaaS); periodic compliance reports demonstrate due diligence and catch anomalies before they become incidents.

---

## Comparison

| Option | Log Format | Tamper-Evident | Captures Reasoning | Best For |
|---|---|---|---|---|
| 1. Structured JSON audit | JSON per event | No | No | All agents — baseline audit log |
| 2. Human-readable trail | Text report | No | No | Incident investigation |
| 3. Sensitivity-tiered | Varies by level | No | No | Mixed-sensitivity tool sets |
| 4. Hash-chained log | JSON + hash | Yes | No | Compliance, financial, healthcare |
| 5. Reasoning capture | Text blocks | No | Yes | High-autonomy agents |
| 6. Compliance reporter | LLM summary | N/A | Partial | Regulated industries, reporting |
