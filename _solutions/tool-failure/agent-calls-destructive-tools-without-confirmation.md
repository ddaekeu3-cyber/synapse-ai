---
layout: solution
title: "Agent Calls Destructive Tools Without Confirmation"
category: tool-failure
description: "The agent deletes files, sends emails, drops database records, or publishes content without asking the user first. One misunderstood instruction causes irreversible damage."
tags: [tool-use, confirmation, destructive, safety, dry-run, reversibility, human-in-the-loop]
---

## Symptom

A user says "clean up old files." The agent interprets this broadly and calls `delete_file()` on 200 files — including ones the user wanted to keep. Or a user says "send the report" and the agent emails it to the entire distribution list before the user had a chance to review it. The action is irreversible, and the agent had no confirmation gate.

## Root Cause

Destructive tool calls — delete, send, publish, overwrite, drop — are treated identically to read-only calls. There is no classification of tool dangerousness, no dry-run mode, and no confirmation checkpoint. The agent optimizes for completing the task, not for preserving reversibility.

## Fix

---

### Option 1: Danger Classification with Mandatory Confirmation

Tag each tool as `safe`, `caution`, or `destructive`. Require explicit confirmation before executing `destructive` tools.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Tool danger levels
TOOL_DANGER = {
    "read_file":          "safe",
    "list_directory":     "safe",
    "search_files":       "safe",
    "write_file":         "caution",     # reversible if backed up
    "create_directory":   "caution",
    "delete_file":        "destructive",
    "delete_directory":   "destructive",
    "send_email":         "destructive",
    "publish_post":       "destructive",
    "drop_table":         "destructive",
    "execute_sql":        "caution",
}

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "delete_file",
        "description": "Permanently delete a file from the filesystem.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "send_email",
        "description": "Send an email to one or more recipients.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def confirm_destructive(tool_name: str, args: dict) -> bool:
    """Ask for human confirmation before executing a destructive tool."""
    danger = TOOL_DANGER.get(tool_name, "safe")

    if danger == "safe":
        return True

    if danger == "caution":
        print(f"\n⚠️  CAUTION: {tool_name}({json.dumps(args, indent=2)})")
        response = input("   Proceed? (y/n): ").strip().lower()
        return response == "y"

    if danger == "destructive":
        print(f"\n🔴 DESTRUCTIVE ACTION REQUIRED: {tool_name}")
        print(f"   Arguments: {json.dumps(args, indent=2)}")
        print(f"   This action CANNOT be undone.")
        response = input("   Type 'YES' to confirm, anything else to cancel: ").strip()
        return response == "YES"

    return True


def simulate_tool(tool_name: str, args: dict, dry_run: bool = False) -> dict:
    """Simulate tool execution (or preview in dry-run mode)."""
    if dry_run:
        return {"status": "dry_run", "would_execute": tool_name, "args": args}

    if tool_name == "read_file":
        return {"content": f"[Contents of {args['path']}]", "size": 1024}
    elif tool_name == "delete_file":
        return {"status": "deleted", "path": args["path"]}
    elif tool_name == "send_email":
        return {"status": "sent", "to": args["to"], "message_id": "msg_001"}

    return {"status": "ok"}


def run_safe_agent(user_message: str, auto_confirm: bool = False) -> str:
    """
    Agent that requires confirmation for destructive actions.
    Set auto_confirm=True only for testing.
    """
    messages = [{"role": "user", "content": user_message}]
    confirmed_actions = []
    blocked_actions = []

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next(b.text for b in response.content if b.type == "text")
            if blocked_actions:
                reply += f"\n\n(Blocked actions: {blocked_actions})"
            return reply

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            danger = TOOL_DANGER.get(block.name, "safe")
            approved = auto_confirm or confirm_destructive(block.name, block.input)

            if approved:
                result = simulate_tool(block.name, block.input)
                confirmed_actions.append(block.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
            else:
                blocked_actions.append(block.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({
                        "error": "Action cancelled by user.",
                        "tool": block.name,
                    }),
                    "is_error": True,
                })

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


# In automated tests, use auto_confirm=True
result = run_safe_agent("Read config.txt, then delete old_backup.log, then email alice@example.com", auto_confirm=True)
print(result)
```

**Expected Token Savings**: Prevents catastrophic irreversible actions — one blocked delete or unsent email can save hours of recovery work.
**Environment**: Synchronous Python with stdin confirmation. In web UIs, replace `input()` with a confirmation modal.

---

### Option 2: Dry-Run Mode — Preview All Side Effects Before Executing

Add a `dry_run` parameter to every tool. Run in dry-run mode first, show the plan, then execute only after approval.

```python
import json
import anthropic

client = anthropic.Anthropic()

# All tools support dry_run parameter
TOOLS = [
    {
        "name": "delete_files",
        "description": "Delete one or more files. In dry_run=true mode, only shows what would be deleted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "paths":    {"type": "array", "items": {"type": "string"}},
                "dry_run":  {"type": "boolean", "default": True, "description": "If true, preview only — don't actually delete"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "send_emails",
        "description": "Send emails to recipients. In dry_run=true mode, only shows what would be sent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipients": {"type": "array", "items": {"type": "string"}},
                "subject":    {"type": "string"},
                "body":       {"type": "string"},
                "dry_run":    {"type": "boolean", "default": True},
            },
            "required": ["recipients", "subject", "body"],
        },
    },
    {
        "name": "execute_sql",
        "description": "Execute a SQL statement. In dry_run=true, returns the query and affected row count estimate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
]


def execute_tool(name: str, args: dict) -> dict:
    dry = args.get("dry_run", True)

    if name == "delete_files":
        if dry:
            return {
                "dry_run": True,
                "would_delete": args["paths"],
                "count": len(args["paths"]),
                "warning": "These files will be permanently deleted.",
            }
        return {"deleted": args["paths"], "count": len(args["paths"])}

    elif name == "send_emails":
        if dry:
            return {
                "dry_run": True,
                "would_send_to": args["recipients"],
                "subject": args["subject"],
                "body_preview": args["body"][:100] + "...",
            }
        return {"sent": True, "recipients": args["recipients"]}

    elif name == "execute_sql":
        if dry:
            query = args["query"].strip().upper()
            op = query.split()[0] if query else "UNKNOWN"
            return {
                "dry_run": True,
                "query": args["query"],
                "operation": op,
                "estimated_rows_affected": 42 if op in ("DELETE", "UPDATE") else 0,
                "warning": "Review this query before executing." if op in ("DELETE", "DROP", "TRUNCATE") else None,
            }
        return {"rows_affected": 42, "status": "ok"}

    return {"error": "unknown tool"}


def run_dry_then_confirm(user_message: str) -> str:
    """
    Phase 1: Run all tools in dry_run mode to show the plan.
    Phase 2: Ask user to confirm, then execute for real.
    """
    dry_plan = []

    # Phase 1: Dry run
    print("=== DRY RUN PHASE ===")
    messages = [{"role": "user", "content": f"[DRY RUN - preview only, don't actually execute] {user_message}"}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=TOOLS,
        system="Always use dry_run=true unless explicitly told to execute for real.",
        messages=messages,
    )

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            # Force dry_run=True
            args = {**block.input, "dry_run": True}
            result = execute_tool(block.name, args)
            dry_plan.append({"tool": block.name, "args": args, "preview": result})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            print(f"  Would execute {block.name}: {json.dumps(result, indent=2)}")

    if not dry_plan:
        return "No actions needed."

    # Phase 2: Confirm
    print("\n=== EXECUTION PLAN ===")
    for i, item in enumerate(dry_plan):
        print(f"  {i+1}. {item['tool']}: {json.dumps(item['preview'])}")

    confirm = input("\nExecute all these actions? (yes/no): ").strip().lower()
    if confirm != "yes":
        return "Execution cancelled. No changes were made."

    # Phase 3: Execute for real
    print("\n=== EXECUTING ===")
    for item in dry_plan:
        args = {**item["args"], "dry_run": False}
        result = execute_tool(item["tool"], args)
        print(f"  ✓ {item['tool']}: {result}")

    return f"Executed {len(dry_plan)} action(s) successfully."


result = run_dry_then_confirm("Delete test_*.log files and notify alice@example.com that cleanup is done.")
print(f"\nResult: {result}")
```

**Expected Token Savings**: Dry-run phase shows full action plan with zero side effects. Users cancel bad plans before any damage occurs.
**Environment**: Two-phase execution. Adapt `input()` to web modal for production UIs.

---

### Option 3: Reversibility Wrapper — Auto-Backup Before Destructive Operations

Automatically create a backup/snapshot before any destructive action. Enable one-command undo.

```python
import json
import shutil
import hashlib
import time
from pathlib import Path
import anthropic

client = anthropic.Anthropic()

UNDO_LOG: list[dict] = []
BACKUP_DIR = Path(".agent_backups")
BACKUP_DIR.mkdir(exist_ok=True)


def backup_file(path: str) -> str | None:
    """Create a timestamped backup. Returns backup path or None if file doesn't exist."""
    src = Path(path)
    if not src.exists():
        return None
    ts = int(time.time())
    backup_path = BACKUP_DIR / f"{src.name}.{ts}.bak"
    shutil.copy2(src, backup_path)
    return str(backup_path)


def undo_last() -> str:
    """Undo the most recent reversible action."""
    if not UNDO_LOG:
        return "Nothing to undo."

    entry = UNDO_LOG.pop()
    action = entry["action"]

    if action == "delete_file" and entry.get("backup"):
        shutil.copy2(entry["backup"], entry["original"])
        return f"Restored {entry['original']} from backup."
    elif action == "write_file" and entry.get("backup"):
        shutil.copy2(entry["backup"], entry["original"])
        return f"Restored previous version of {entry['original']}."
    elif action == "write_file" and not entry.get("had_previous"):
        Path(entry["original"]).unlink(missing_ok=True)
        return f"Deleted newly created file {entry['original']}."
    else:
        return f"Cannot undo action: {action} (no backup available)."


TOOLS = [
    {
        "name": "delete_file",
        "description": "Delete a file. A backup is automatically created for recovery.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write content to a file (overwrites if exists). Previous version is backed up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "undo_last_action",
        "description": "Undo the most recent file operation.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def safe_execute(tool_name: str, args: dict) -> dict:
    """Execute tool with automatic backup for reversibility."""
    if tool_name == "delete_file":
        path = args["path"]
        backup = backup_file(path)
        if backup:
            Path(path).unlink()
            UNDO_LOG.append({"action": "delete_file", "original": path, "backup": backup})
            return {"deleted": path, "backup": backup, "undo": "Call undo_last_action to restore."}
        return {"error": f"File not found: {path}"}

    elif tool_name == "write_file":
        path = args["path"]
        had_previous = Path(path).exists()
        backup = backup_file(path) if had_previous else None
        Path(path).write_text(args["content"])
        UNDO_LOG.append({
            "action": "write_file",
            "original": path,
            "backup": backup,
            "had_previous": had_previous,
        })
        return {"written": path, "backup": backup or "none (new file)", "undo": "Call undo_last_action."}

    elif tool_name == "undo_last_action":
        return {"result": undo_last()}

    return {"error": "unknown tool"}


def run_reversible_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = safe_execute(block.name, block.input)
                print(f"  {block.name}: {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


run_reversible_agent("Write 'hello world' to test.txt, then delete old_config.txt")
run_reversible_agent("Actually, undo that last deletion.")
```

**Expected Token Savings**: Automatic backups make destructive calls safe to execute without confirmation delays — faster flow, zero fear of data loss.
**Environment**: Local file system. For databases, implement as SQL transaction rollback.

---

### Option 4: Scope Limiter — Restrict Destructive Operations to Declared Scope

Require the agent to declare its scope upfront. Block any destructive call that exceeds the declared scope.

```python
import json
import re
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class AgentScope:
    allowed_paths: list[str] = field(default_factory=list)  # file path prefixes
    allowed_recipients: list[str] = field(default_factory=list)  # email domains or addresses
    max_files_per_batch: int = 10
    can_delete: bool = False
    can_send_external: bool = False


SCOPE_DECLARATION_TOOL = {
    "name": "declare_scope",
    "description": "Declare the scope of work before executing any destructive operations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "affected_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths or prefixes that will be modified or deleted",
            },
            "estimated_file_count": {"type": "integer"},
            "will_delete": {"type": "boolean"},
            "will_send_to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses or domains that will be contacted",
            },
            "justification": {"type": "string"},
        },
        "required": ["affected_paths", "estimated_file_count", "will_delete", "justification"],
    },
}

TOOLS = [
    SCOPE_DECLARATION_TOOL,
    {
        "name": "delete_file",
        "description": "Delete a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
]


def validate_scope(scope: AgentScope, tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if a tool call is within the declared scope."""
    if tool_name == "delete_file":
        if not scope.can_delete:
            return False, "Scope does not permit file deletion. Declare scope with will_delete=true first."
        path = args.get("path", "")
        if scope.allowed_paths and not any(path.startswith(p) for p in scope.allowed_paths):
            return False, f"Path '{path}' is outside declared scope: {scope.allowed_paths}"

    if tool_name == "send_email":
        if not scope.can_send_external:
            return False, "Scope does not permit sending emails."
        to = args.get("to", "")
        if scope.allowed_recipients and not any(to.endswith(r) for r in scope.allowed_recipients):
            return False, f"Recipient '{to}' is outside declared scope."

    return True, ""


def run_scoped_agent(message: str, user_scope: AgentScope) -> str:
    declared_scope = None
    messages = [{"role": "user", "content": message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="Always declare your scope using declare_scope before any destructive operations.",
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "declare_scope":
                # Process scope declaration
                inp = block.input
                declared_scope = AgentScope(
                    allowed_paths=inp.get("affected_paths", []),
                    max_files_per_batch=inp.get("estimated_file_count", 1),
                    can_delete=inp.get("will_delete", False),
                    allowed_recipients=inp.get("will_send_to", []),
                )
                print(f"  [Scope declared] paths={declared_scope.allowed_paths}, can_delete={declared_scope.can_delete}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"scope_accepted": True})})
                continue

            # Check scope for destructive tools
            active_scope = declared_scope or user_scope
            allowed, reason = validate_scope(active_scope, block.name, block.input)

            if not allowed:
                print(f"  [Scope violation blocked] {block.name}: {reason}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": f"Scope violation: {reason}"}),
                    "is_error": True,
                })
            else:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                               "content": json.dumps({"status": "executed", "tool": block.name})})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


# User grants limited scope
user_scope = AgentScope(
    allowed_paths=["./logs/"],
    can_delete=True,
    max_files_per_batch=5,
)
result = run_scoped_agent("Clean up all log files in the logs directory", user_scope)
print(result)
```

**Expected Token Savings**: Scope violations are caught without executing the tool — zero side effects for out-of-scope requests. Scope declaration adds ~1 cheap tool call upfront.
**Environment**: In-memory scope tracking. Extend to database-backed scopes for multi-session agents.

---

### Option 5: Consequence Severity Scoring Before Execution

Score the severity of each planned action (1–10). Gate execution on severity threshold.

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SEVERITY_TOOL = {
    "name": "assess_severity",
    "description": "Assess the severity and reversibility of a planned tool call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "severity_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "1=safe read, 5=reversible write, 10=irreversible destruction",
            },
            "is_reversible": {"type": "boolean"},
            "affected_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What will be affected (file names, email addresses, table names, etc.)",
            },
            "worst_case": {"type": "string", "description": "What's the worst thing that could happen?"},
            "recommendation": {"type": "string", "enum": ["proceed", "warn_user", "require_confirmation", "block"]},
        },
        "required": ["severity_score", "is_reversible", "affected_entities", "worst_case", "recommendation"],
    },
}

USER_SEVERITY_THRESHOLD = 6  # Auto-block if score >= this
WARN_THRESHOLD = 4


async def assess_action(tool_name: str, args: dict) -> dict:
    """Use LLM to assess the severity of a tool call."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[SEVERITY_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                f"Assess the severity of this tool call:\n"
                f"Tool: {tool_name}\n"
                f"Args: {json.dumps(args)}"
            ),
        }],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"severity_score": 5, "is_reversible": True, "affected_entities": [], "worst_case": "unknown", "recommendation": "warn_user"}


TOOLS = [
    {"name": "delete_records", "description": "Delete database records matching a filter.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}, "where": {"type": "string"}}, "required": ["table", "where"]}},
    {"name": "send_newsletter", "description": "Send newsletter to all subscribers.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "content": {"type": "string"}}, "required": ["subject", "content"]}},
]


async def run_severity_gated_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        # Assess all actions in parallel
        assessments = await asyncio.gather(*[
            assess_action(b.name, b.input) for b in tool_blocks
        ])

        results = []
        for block, assessment in zip(tool_blocks, assessments):
            score = assessment["severity_score"]
            rec = assessment["recommendation"]

            print(f"  [{block.name}] Severity: {score}/10 | {rec} | Worst case: {assessment['worst_case'][:60]}")

            if score >= USER_SEVERITY_THRESHOLD or rec == "block":
                print(f"  🔴 BLOCKED — Severity {score} exceeds threshold {USER_SEVERITY_THRESHOLD}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({
                        "error": f"Action blocked (severity {score}/10). Worst case: {assessment['worst_case']}. Requires explicit user confirmation.",
                        "affected": assessment["affected_entities"],
                    }),
                    "is_error": True,
                })
            elif score >= WARN_THRESHOLD or rec in ("warn_user", "require_confirmation"):
                print(f"  ⚠️  Warning issued to user")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                               "content": json.dumps({"status": "executed_with_warning", "severity": score})})
            else:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                               "content": json.dumps({"status": "executed"})})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


asyncio.run(run_severity_gated_agent("Send the monthly newsletter and delete old inactive accounts."))
```

**Expected Token Savings**: Severity assessment (Haiku, ~50 tokens) runs in parallel with the main call — minimal latency overhead. High-severity actions blocked before execution.
**Environment**: Async Python. Tune `USER_SEVERITY_THRESHOLD` per deployment risk tolerance.

---

### Option 6: Immutable Audit Trail for All Destructive Actions

Log every destructive action with full context. Enable post-hoc review and accountability.

```python
import json
import sqlite3
import time
import hashlib
from datetime import datetime, timezone
import anthropic

client = anthropic.Anthropic()

DESTRUCTIVE_TOOLS = {"delete_file", "send_email", "drop_table", "publish_content", "execute_sql"}

audit_conn = sqlite3.connect("agent_audit.db")
audit_conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT,
        session_id  TEXT,
        tool_name   TEXT,
        args_hash   TEXT,
        args_json   TEXT,
        result_json TEXT,
        user_message TEXT,
        was_blocked INTEGER DEFAULT 0
    )
""")
audit_conn.commit()


def log_action(session_id: str, tool_name: str, args: dict, result: dict,
               user_message: str, was_blocked: bool = False):
    args_json = json.dumps(args, sort_keys=True)
    args_hash = hashlib.sha256(args_json.encode()).hexdigest()[:16]
    audit_conn.execute(
        "INSERT INTO audit_log (ts, session_id, tool_name, args_hash, args_json, result_json, user_message, was_blocked) VALUES (?,?,?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            session_id,
            tool_name,
            args_hash,
            args_json,
            json.dumps(result),
            user_message[:500],
            int(was_blocked),
        ),
    )
    audit_conn.commit()


def get_audit_report(session_id: str) -> str:
    rows = audit_conn.execute(
        "SELECT ts, tool_name, args_json, was_blocked FROM audit_log WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    if not rows:
        return "No audit log entries."
    lines = ["Audit Log:"]
    for ts, tool, args, blocked in rows:
        status = "🔴 BLOCKED" if blocked else "✓ EXECUTED"
        lines.append(f"  [{ts[:19]}] {status} {tool}({json.loads(args)})")
    return "\n".join(lines)


TOOLS = [
    {"name": "delete_file",      "description": "Delete a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "send_email",       "description": "Send an email.", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}}, "required": ["to", "subject"]}},
    {"name": "read_file",        "description": "Read a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]


def run_audited_agent(user_message: str, session_id: str, block_destructive: bool = False) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            is_destructive = block.name in DESTRUCTIVE_TOOLS
            blocked = is_destructive and block_destructive

            if blocked:
                result = {"error": f"Tool '{block.name}' is destructive and was blocked in this session."}
                print(f"  [BLOCKED] {block.name}")
            else:
                result = {"status": "executed", "tool": block.name}
                print(f"  [EXECUTED] {block.name}({block.input})")

            # Always log — even blocked actions
            log_action(session_id, block.name, block.input, result, user_message, was_blocked=blocked)

            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
                **({"is_error": True} if blocked else {}),
            })

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


session = "sess_001"
run_audited_agent("Read config.txt and delete old_data.csv", session_id=session, block_destructive=False)
run_audited_agent("Send a welcome email to new_user@example.com", session_id=session, block_destructive=True)

print("\n" + get_audit_report(session))
```

**Expected Token Savings**: Audit trail enables fast post-hoc investigation — no need for expensive replay sessions. `block_destructive=True` runs agents in read-only mode for safe testing.
**Environment**: SQLite audit log. For compliance, make the table append-only (no UPDATE/DELETE on audit_log).

---

| Option | Gate Mechanism | User Friction | Reversibility | Best For |
|--------|---------------|--------------|--------------|---------|
| 1 | Danger classification | Confirm per action | None | Interactive agents |
| 2 | Dry-run preview | One confirm for all | None | Batch operations |
| 3 | Auto-backup | None | Full undo | File/DB operations |
| 4 | Scope declaration | Upfront scope | None | Bounded task agents |
| 5 | Severity scoring | Blocked at threshold | None | Risk-proportional gating |
| 6 | Audit trail | None (or block mode) | Post-hoc review | Compliance, accountability |
