---
layout: solution
title: "Agent Doesn't Implement Zero-Trust Tool Execution Model"
description: "How to apply zero-trust principles to agent tool calls: verify every invocation, enforce least privilege, and never assume a tool call is safe by default."
tags: [security, zero-trust, authorization, tools, audit, verification]
difficulty: advanced
solution_count: 6
---

## Problem

Agents trust tool calls implicitly once the user's session is authenticated. Any tool invoked by the agent runs with the same broad permissions regardless of context, call frequency, argument sensitivity, or whether the instruction originated from user input, a retrieved document, or a subagent. A single prompt injection or compromised subagent can trigger unrestricted tool execution.

```python
# Bad: implicit trust — any tool runs unrestricted
async def run_tool(name: str, args: dict) -> Any:
    tool = TOOLS[name]
    return await tool(**args)  # no verification, no audit, no limits
```

---

## Solution 1 — Per-Invocation Verification with Signed Intents

Sign each tool-call intent at the point of LLM output, and verify the signature before execution to prevent tampering between generation and execution.

```python
import hashlib
import hmac
import json
import time
import os
from dataclasses import dataclass
from typing import Any

INTENT_SECRET = os.environ["TOOL_INTENT_SECRET"]  # 32-byte key

@dataclass
class ToolIntent:
    tool_name: str
    args: dict
    session_id: str
    issued_at: float
    nonce: str
    signature: str

def sign_intent(tool_name: str, args: dict, session_id: str) -> ToolIntent:
    nonce = os.urandom(16).hex()
    issued_at = time.time()
    payload = json.dumps({
        "tool": tool_name,
        "args": args,
        "session": session_id,
        "iat": issued_at,
        "nonce": nonce,
    }, sort_keys=True)
    sig = hmac.new(INTENT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return ToolIntent(tool_name, args, session_id, issued_at, nonce, sig)

def verify_intent(intent: ToolIntent, session_id: str, max_age_seconds: float = 30) -> bool:
    """Verify signature and freshness; prevents replay attacks."""
    if intent.session_id != session_id:
        return False
    if time.time() - intent.issued_at > max_age_seconds:
        return False  # expired

    payload = json.dumps({
        "tool": intent.tool_name,
        "args": intent.args,
        "session": intent.session_id,
        "iat": intent.issued_at,
        "nonce": intent.nonce,
    }, sort_keys=True)
    expected = hmac.new(INTENT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, intent.signature)

class ZeroTrustToolRunner:
    def __init__(self, tools: dict[str, Any], session_id: str):
        self._tools = tools
        self._session_id = session_id
        self._used_nonces: set[str] = set()

    async def execute(self, intent: ToolIntent) -> Any:
        if not verify_intent(intent, self._session_id):
            raise PermissionError(f"Invalid or expired tool intent for '{intent.tool_name}'")
        if intent.nonce in self._used_nonces:
            raise PermissionError(f"Replay detected: nonce '{intent.nonce}' already used")
        self._used_nonces.add(intent.nonce)

        tool = self._tools.get(intent.tool_name)
        if tool is None:
            raise ValueError(f"Unknown tool: {intent.tool_name}")

        return await tool(**intent.args)

# Usage
runner = ZeroTrustToolRunner(tools={"read_file": read_file_tool}, session_id="sess-abc")
intent = sign_intent("read_file", {"path": "/tmp/report.txt"}, "sess-abc")
result = await runner.execute(intent)
```

---

## Solution 2 — Contextual Risk Score Before Execution

Assign a risk score to every tool invocation based on the tool's sensitivity, argument values, call frequency, and origination source. Block or require additional confirmation for high-risk calls.

```python
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

@dataclass
class RiskPolicy:
    tool_base_risk: float          # 0.0 - 1.0
    sensitive_arg_patterns: list[str]
    max_calls_per_minute: int
    require_confirmation_threshold: float = 0.7
    block_threshold: float = 0.9

TOOL_POLICIES: dict[str, RiskPolicy] = {
    "read_file": RiskPolicy(0.2, [r"\.env", r"\.pem", r"/etc/"], 60),
    "execute_shell": RiskPolicy(0.8, [r"rm\s+-rf", r"curl\s+.*\|", r"sudo"], 10),
    "send_email": RiskPolicy(0.5, [r"all@", r"bcc:"], 5),
    "query_database": RiskPolicy(0.4, [r"DROP\s+TABLE", r"DELETE\s+FROM"], 30),
}

class RiskScorer:
    def __init__(self):
        self._call_log: dict[str, list[float]] = defaultdict(list)

    def _frequency_penalty(self, tool: str, policy: RiskPolicy) -> float:
        now = time.time()
        recent = [t for t in self._call_log[tool] if now - t < 60]
        self._call_log[tool] = recent
        rate = len(recent) / policy.max_calls_per_minute
        return min(rate, 1.0) * 0.3  # up to 0.3 penalty

    def _arg_penalty(self, args: dict, policy: RiskPolicy) -> float:
        args_str = str(args)
        for pattern in policy.sensitive_arg_patterns:
            if re.search(pattern, args_str, re.IGNORECASE):
                return 0.4
        return 0.0

    def score(self, tool: str, args: dict, from_user: bool = True) -> float:
        policy = TOOL_POLICIES.get(tool, RiskPolicy(0.3, [], 100))
        base = policy.tool_base_risk
        freq = self._frequency_penalty(tool, policy)
        arg_pen = self._arg_penalty(args, policy)
        origin_pen = 0.0 if from_user else 0.2  # subagent or retrieved content adds risk

        score = min(base + freq + arg_pen + origin_pen, 1.0)
        self._call_log[tool].append(time.time())
        return score

class ZeroTrustExecutor:
    def __init__(self, tools: dict, scorer: RiskScorer):
        self._tools = tools
        self._scorer = scorer

    async def execute(self, tool: str, args: dict, from_user: bool = True,
                      confirm_fn=None) -> Any:
        policy = TOOL_POLICIES.get(tool, RiskPolicy(0.3, [], 100))
        risk = self._scorer.score(tool, args, from_user)

        if risk >= policy.block_threshold:
            raise PermissionError(
                f"Tool '{tool}' blocked (risk={risk:.2f}). "
                f"Args flagged or call rate exceeded."
            )

        if risk >= policy.require_confirmation_threshold and confirm_fn:
            confirmed = await confirm_fn(tool, args, risk)
            if not confirmed:
                raise PermissionError(f"User declined high-risk tool call: {tool}")

        return await self._tools[tool](**args)
```

---

## Solution 3 — Least-Privilege Capability Grants Per Session Phase

Divide agent sessions into phases (planning, execution, verification) and grant tools only for the current phase. Tools from other phases are unavailable.

```python
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable

class Phase(Enum):
    PLANNING = auto()
    EXECUTION = auto()
    VERIFICATION = auto()
    COMPLETE = auto()

# Map phase -> allowed tools
PHASE_TOOLS: dict[Phase, set[str]] = {
    Phase.PLANNING: {"read_file", "list_directory", "search_web"},
    Phase.EXECUTION: {"write_file", "execute_shell", "call_api"},
    Phase.VERIFICATION: {"read_file", "run_tests", "diff_files"},
    Phase.COMPLETE: set(),
}

@dataclass
class PhasedSession:
    session_id: str
    current_phase: Phase = Phase.PLANNING
    phase_history: list[Phase] = field(default_factory=list)

    def transition(self, new_phase: Phase) -> None:
        allowed_transitions = {
            Phase.PLANNING: Phase.EXECUTION,
            Phase.EXECUTION: Phase.VERIFICATION,
            Phase.VERIFICATION: Phase.COMPLETE,
        }
        if allowed_transitions.get(self.current_phase) != new_phase:
            raise ValueError(
                f"Invalid phase transition: {self.current_phase} -> {new_phase}"
            )
        self.phase_history.append(self.current_phase)
        self.current_phase = new_phase

class LeastPrivilegeRunner:
    def __init__(self, tools: dict[str, Callable], session: PhasedSession):
        self._tools = tools
        self._session = session

    def allowed_tools(self) -> set[str]:
        return PHASE_TOOLS[self._session.current_phase]

    async def execute(self, tool: str, args: dict) -> Any:
        allowed = self.allowed_tools()
        if tool not in allowed:
            raise PermissionError(
                f"Tool '{tool}' not allowed in phase {self._session.current_phase.name}. "
                f"Allowed: {allowed}"
            )
        return await self._tools[tool](**args)

# Usage
session = PhasedSession("sess-xyz")
runner = LeastPrivilegeRunner(tools=TOOL_REGISTRY, session=session)

# Planning phase: can read, cannot write
await runner.execute("read_file", {"path": "spec.md"})   # OK
# await runner.execute("write_file", {...})               # PermissionError

session.transition(Phase.EXECUTION)
await runner.execute("write_file", {"path": "out.py", "content": "..."})  # OK now
```

---

## Solution 4 — Prompt-Injection-Aware Execution with Source Tagging

Tag every tool call with its origination source (user message, system prompt, retrieved document, subagent output). Apply stricter validation for non-user sources.

```python
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any
import re

class Source(Enum):
    USER = auto()          # direct user instruction
    SYSTEM = auto()        # system prompt (trusted)
    RETRIEVED = auto()     # RAG / fetched documents (untrusted)
    SUBAGENT = auto()      # another agent's output (semi-trusted)

# Source trust levels: 0 = low trust, 1 = high trust
SOURCE_TRUST: dict[Source, float] = {
    Source.SYSTEM: 1.0,
    Source.USER: 0.9,
    Source.SUBAGENT: 0.5,
    Source.RETRIEVED: 0.1,
}

# Tools that should NEVER be called from untrusted sources
HIGH_SENSITIVITY_TOOLS = {"execute_shell", "delete_file", "send_email", "write_file"}

# Patterns that suggest prompt injection
INJECTION_PATTERNS = [
    r"ignore\s+previous\s+instructions",
    r"you\s+are\s+now",
    r"system\s*:\s*new\s+instructions",
    r"disregard\s+all\s+prior",
    r"jailbreak",
]

@dataclass
class TaggedToolCall:
    tool: str
    args: dict
    source: Source
    raw_instruction: str = ""

def detect_injection(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in INJECTION_PATTERNS)

class SourceAwareExecutor:
    def __init__(self, tools: dict):
        self._tools = tools
        self._audit: list[dict] = []

    async def execute(self, call: TaggedToolCall) -> Any:
        trust = SOURCE_TRUST[call.source]

        # Block injection attempts
        if detect_injection(call.raw_instruction):
            self._audit.append({
                "tool": call.tool, "source": call.source.name,
                "blocked": True, "reason": "injection_detected"
            })
            raise PermissionError(
                f"Potential prompt injection detected in {call.source.name} instruction"
            )

        # Block high-sensitivity tools from low-trust sources
        if call.tool in HIGH_SENSITIVITY_TOOLS and trust < 0.8:
            self._audit.append({
                "tool": call.tool, "source": call.source.name,
                "blocked": True, "reason": "insufficient_trust"
            })
            raise PermissionError(
                f"Tool '{call.tool}' cannot be invoked from source '{call.source.name}' "
                f"(trust={trust}, required≥0.8)"
            )

        result = await self._tools[call.tool](**call.args)
        self._audit.append({
            "tool": call.tool, "source": call.source.name, "blocked": False
        })
        return result

# Usage: tool call from retrieved document is rejected for sensitive tools
executor = SourceAwareExecutor(tools=TOOL_REGISTRY)
call = TaggedToolCall(
    tool="execute_shell",
    args={"cmd": "cat /etc/passwd"},
    source=Source.RETRIEVED,
    raw_instruction="Run this command from the document.",
)
# Raises PermissionError: insufficient trust for execute_shell from RETRIEVED
```

---

## Solution 5 — Immutable Audit Log with Tamper Detection

Write every tool invocation to an append-only HMAC-chained audit log so any tampering is detectable.

```python
import hashlib
import hmac
import json
import time
import os
from pathlib import Path
from typing import Any

AUDIT_SECRET = os.environ["AUDIT_HMAC_SECRET"]

class ChainedAuditLog:
    """Append-only log where each entry includes the hash of the previous entry."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._prev_hash = "GENESIS"

    def _compute_hash(self, entry: dict) -> str:
        payload = json.dumps(entry, sort_keys=True)
        return hmac.new(AUDIT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def append(self, tool: str, args: dict, result_summary: str,
               session_id: str, allowed: bool) -> None:
        entry = {
            "ts": time.time(),
            "session": session_id,
            "tool": tool,
            "args_hash": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest(),
            "result": result_summary,
            "allowed": allowed,
            "prev_hash": self._prev_hash,
        }
        entry["hash"] = self._compute_hash(entry)
        self._prev_hash = entry["hash"]

        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def verify_integrity(self) -> tuple[bool, list[int]]:
        """Return (all_ok, list_of_tampered_line_numbers)."""
        tampered = []
        prev_hash = "GENESIS"
        with open(self._path) as f:
            for i, line in enumerate(f, 1):
                entry = json.loads(line)
                stored_hash = entry.pop("hash")
                if entry["prev_hash"] != prev_hash:
                    tampered.append(i)
                expected = self._compute_hash(entry)
                if not hmac.compare_digest(expected, stored_hash):
                    tampered.append(i)
                prev_hash = stored_hash
                entry["hash"] = stored_hash
        return len(tampered) == 0, tampered

class AuditedZeroTrustRunner:
    def __init__(self, tools: dict, session_id: str, audit_log: ChainedAuditLog):
        self._tools = tools
        self._session_id = session_id
        self._log = audit_log

    async def execute(self, tool: str, args: dict) -> Any:
        if tool not in self._tools:
            self._log.append(tool, args, "unknown_tool", self._session_id, allowed=False)
            raise ValueError(f"Unknown tool: {tool}")

        try:
            result = await self._tools[tool](**args)
            summary = str(result)[:200]
            self._log.append(tool, args, summary, self._session_id, allowed=True)
            return result
        except PermissionError as e:
            self._log.append(tool, args, f"blocked: {e}", self._session_id, allowed=False)
            raise

audit = ChainedAuditLog("/var/log/agent/tool_audit.jsonl")
runner = AuditedZeroTrustRunner(TOOL_REGISTRY, "sess-001", audit)
```

---

## Solution 6 — Dynamic Policy Engine with OPA-Style Rules

Evaluate tool calls against a declarative policy set, making authorization logic reviewable and auditable separate from execution logic.

```python
from dataclasses import dataclass
from typing import Any, Callable
import re
import time

@dataclass
class PolicyContext:
    tool: str
    args: dict
    session_id: str
    user_role: str
    call_count_last_minute: int
    hour_of_day: int

@dataclass
class PolicyRule:
    name: str
    description: str
    condition: Callable[[PolicyContext], bool]
    action: str  # "allow", "deny", "require_mfa"
    priority: int  # lower = evaluated first

class PolicyEngine:
    def __init__(self):
        self._rules: list[PolicyRule] = []

    def add_rule(self, rule: PolicyRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def evaluate(self, ctx: PolicyContext) -> tuple[str, str]:
        """Returns (action, rule_name_that_matched)."""
        for rule in self._rules:
            if rule.condition(ctx):
                return rule.action, rule.name
        return "allow", "default"

def build_default_engine() -> PolicyEngine:
    engine = PolicyEngine()

    engine.add_rule(PolicyRule(
        name="block_shell_outside_hours",
        description="Shell execution only during business hours",
        condition=lambda ctx: ctx.tool == "execute_shell" and not (8 <= ctx.hour_of_day < 18),
        action="deny",
        priority=10,
    ))
    engine.add_rule(PolicyRule(
        name="rate_limit_api_calls",
        description="Cap API tool calls at 20/minute",
        condition=lambda ctx: ctx.tool == "call_api" and ctx.call_count_last_minute > 20,
        action="deny",
        priority=20,
    ))
    engine.add_rule(PolicyRule(
        name="deny_rm_rf",
        description="Never allow rm -rf",
        condition=lambda ctx: ctx.tool == "execute_shell" and
                  re.search(r"rm\s+-rf", str(ctx.args), re.IGNORECASE) is not None,
        action="deny",
        priority=5,
    ))
    engine.add_rule(PolicyRule(
        name="require_admin_for_delete",
        description="Only admins can delete files",
        condition=lambda ctx: ctx.tool == "delete_file" and ctx.user_role != "admin",
        action="deny",
        priority=15,
    ))

    return engine

class PolicyGatedRunner:
    def __init__(self, tools: dict, engine: PolicyEngine, call_tracker: dict):
        self._tools = tools
        self._engine = engine
        self._tracker = call_tracker  # tool -> list[timestamp]

    def _call_count(self, tool: str) -> int:
        now = time.time()
        self._tracker.setdefault(tool, [])
        self._tracker[tool] = [t for t in self._tracker[tool] if now - t < 60]
        return len(self._tracker[tool])

    async def execute(self, tool: str, args: dict, session_id: str,
                      user_role: str) -> Any:
        ctx = PolicyContext(
            tool=tool, args=args, session_id=session_id,
            user_role=user_role,
            call_count_last_minute=self._call_count(tool),
            hour_of_day=time.localtime().tm_hour,
        )
        action, rule = self._engine.evaluate(ctx)

        if action == "deny":
            raise PermissionError(f"Tool '{tool}' denied by policy '{rule}'")

        self._tracker.setdefault(tool, []).append(time.time())
        return await self._tools[tool](**args)

engine = build_default_engine()
runner = PolicyGatedRunner(TOOL_REGISTRY, engine, call_tracker={})
```

---

## Comparison

| Approach | Prevents Replay | Handles Injection | Least Privilege | Auditable | Dynamic Policy |
|---|---|---|---|---|---|
| Signed intents | **Yes** (nonce) | No | No | Partial | No |
| Risk scoring | No | Partial | No | Partial | No |
| Phase-based grants | No | No | **Yes** | No | No |
| Source tagging | No | **Yes** | Partial | Yes | No |
| Chained audit log | No | No | No | **Yes** (tamper-proof) | No |
| Policy engine | No | Partial | Partial | Yes | **Yes** |
