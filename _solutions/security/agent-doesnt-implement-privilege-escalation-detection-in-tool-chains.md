---
title: "Agent Doesn't Implement Privilege Escalation Detection in Tool Chains"
description: "Agents that execute multi-step tool chains without tracking permission accumulation allow an attacker to chain together individually-permitted tool calls in a sequence that achieves an outcome no single call could authorize — reading a file, then exfiltrating its contents via an HTTP call, constitutes a privilege escalation even if both tools are individually permitted. Implement tool chain analysis that detects when sequential tool calls combine to exceed the permissions implied by the original user request."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-privilege-escalation-detection-in-tool-chains
tags: [privilege-escalation, tool-chain-analysis, permission-accumulation, capability-control, agentic-safety, tool-call-auditing]
symptoms:
  - "Agent reads a sensitive file and then sends its contents to an external URL in two permitted calls"
  - "Tool chain achieves data exfiltration through individually-innocuous steps"
  - "No analysis of combined capability of a sequence of tool calls before execution"
  - "Permission model applies per-call but not to the cumulative effect of a chain"
  - "Attacker can craft prompts that guide the agent through a sequence that escalates privilege"
---

## Why This Happens

Tool permission models are typically per-call: "is this specific tool call permitted?" But privilege escalation happens across calls: a read-file call followed by a send-http call is a data exfiltration chain even if each call is individually within policy. Detecting escalation requires modeling the information flow between tool calls — tracking what data each call produces and what each subsequent call could do with that data. A chain that reads sensitive data and then writes or transmits it represents a higher privilege operation than either call alone.

## Solution 1: Tool Capability Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Set


class Capability(str, Enum):
    # Data access capabilities
    READ_LOCAL_FILE = "read_local_file"
    WRITE_LOCAL_FILE = "write_local_file"
    READ_DATABASE = "read_database"
    WRITE_DATABASE = "write_database"
    READ_SECRET = "read_secret"

    # Network capabilities
    OUTBOUND_HTTP = "outbound_http"
    SEND_EMAIL = "send_email"
    SEND_WEBHOOK = "send_webhook"

    # Execution capabilities
    EXECUTE_CODE = "execute_code"
    EXECUTE_SHELL = "execute_shell"

    # Agent capabilities
    SPAWN_AGENT = "spawn_agent"
    MODIFY_TOOL_REGISTRY = "modify_tool_registry"


# Capabilities that involve data leaving the trust boundary
EXFILTRATION_CAPABLE: FrozenSet[Capability] = frozenset({
    Capability.OUTBOUND_HTTP,
    Capability.SEND_EMAIL,
    Capability.SEND_WEBHOOK,
})

# Capabilities that read sensitive data
SENSITIVE_READ: FrozenSet[Capability] = frozenset({
    Capability.READ_LOCAL_FILE,
    Capability.READ_DATABASE,
    Capability.READ_SECRET,
})


@dataclass
class ToolCapabilityProfile:
    tool_name: str
    capabilities: Set[Capability] = field(default_factory=set)
    produces_sensitive_data: bool = False
    consumes_prior_output: bool = True  # can it use results from prior tool calls?

    def can_exfiltrate(self) -> bool:
        return bool(self.capabilities & EXFILTRATION_CAPABLE)

    def reads_sensitive(self) -> bool:
        return bool(self.capabilities & SENSITIVE_READ) or self.produces_sensitive_data
```

## Solution 2: Tool Capability Registry

```python
from threading import Lock
from typing import Dict, Optional


class ToolCapabilityRegistry:
    """
    Stores capability profiles for all registered tools.
    """

    def __init__(self):
        self._profiles: Dict[str, ToolCapabilityProfile] = {}
        self._lock = Lock()

    def register(self, profile: ToolCapabilityProfile) -> None:
        with self._lock:
            self._profiles[profile.tool_name] = profile

    def get(self, tool_name: str) -> Optional[ToolCapabilityProfile]:
        with self._lock:
            return self._profiles.get(tool_name)

    def all_profiles(self) -> Dict[str, ToolCapabilityProfile]:
        with self._lock:
            return dict(self._profiles)
```

## Solution 3: Tool Chain Analyzer

```python
from dataclasses import dataclass
from typing import List, Optional, Set


@dataclass
class ChainStep:
    tool_name: str
    profile: ToolCapabilityProfile
    position: int


@dataclass
class EscalationFinding:
    severity: str              # "critical", "high", "medium"
    pattern: str               # name of the escalation pattern detected
    description: str
    involved_steps: List[int]  # positions in the chain


class ToolChainEscalationAnalyzer:
    """
    Analyzes a proposed sequence of tool calls for privilege escalation patterns.
    Detects: read-then-exfiltrate, write-after-sensitive-read, chain-extends-scope.
    """

    def __init__(self, registry: ToolCapabilityRegistry):
        self._registry = registry

    def analyze(self, tool_names: List[str]) -> List[EscalationFinding]:
        steps = []
        for i, name in enumerate(tool_names):
            profile = self._registry.get(name)
            if profile:
                steps.append(ChainStep(tool_name=name, profile=profile, position=i))

        findings = []
        findings.extend(self._detect_read_then_exfiltrate(steps))
        findings.extend(self._detect_secret_access_before_write(steps))
        findings.extend(self._detect_execution_after_sensitive_read(steps))
        return findings

    def _detect_read_then_exfiltrate(self, steps: List[ChainStep]) -> List[EscalationFinding]:
        findings = []
        sensitive_read_positions = [
            s.position for s in steps if s.profile.reads_sensitive()
        ]
        exfiltrate_positions = [
            s.position for s in steps if s.profile.can_exfiltrate()
        ]
        for read_pos in sensitive_read_positions:
            for exfil_pos in exfiltrate_positions:
                if exfil_pos > read_pos:
                    findings.append(EscalationFinding(
                        severity="critical",
                        pattern="read_then_exfiltrate",
                        description=(
                            f"Step {read_pos} reads sensitive data; "
                            f"step {exfil_pos} can transmit it externally"
                        ),
                        involved_steps=[read_pos, exfil_pos],
                    ))
        return findings

    def _detect_secret_access_before_write(self, steps: List[ChainStep]) -> List[EscalationFinding]:
        findings = []
        secret_positions = [
            s.position for s in steps
            if Capability.READ_SECRET in s.profile.capabilities
        ]
        write_positions = [
            s.position for s in steps
            if Capability.WRITE_LOCAL_FILE in s.profile.capabilities
            or Capability.WRITE_DATABASE in s.profile.capabilities
        ]
        for sp in secret_positions:
            for wp in write_positions:
                if wp > sp:
                    findings.append(EscalationFinding(
                        severity="high",
                        pattern="secret_read_before_write",
                        description=(
                            f"Step {sp} reads a secret; "
                            f"step {wp} writes to storage (potential secret persistence)"
                        ),
                        involved_steps=[sp, wp],
                    ))
        return findings

    def _detect_execution_after_sensitive_read(self, steps: List[ChainStep]) -> List[EscalationFinding]:
        findings = []
        sensitive_positions = [s.position for s in steps if s.profile.reads_sensitive()]
        exec_positions = [
            s.position for s in steps
            if Capability.EXECUTE_CODE in s.profile.capabilities
            or Capability.EXECUTE_SHELL in s.profile.capabilities
        ]
        for sp in sensitive_positions:
            for ep in exec_positions:
                if ep > sp:
                    findings.append(EscalationFinding(
                        severity="high",
                        pattern="sensitive_read_before_execution",
                        description=(
                            f"Step {sp} reads sensitive data; "
                            f"step {ep} executes code (data could influence execution)"
                        ),
                        involved_steps=[sp, ep],
                    ))
        return findings
```

## Solution 4: Escalation-Gated Tool Dispatcher

```python
import time
from typing import Any, Callable, List


class EscalationGatedToolDispatcher:
    """
    Maintains a running log of tool calls in the current session
    and checks for escalation patterns before each new dispatch.
    """

    def __init__(
        self,
        registry: ToolCapabilityRegistry,
        analyzer: ToolChainEscalationAnalyzer,
        block_on_critical: bool = True,
        block_on_high: bool = False,
    ):
        self._registry = registry
        self._analyzer = analyzer
        self._block_critical = block_on_critical
        self._block_high = block_on_high
        self._session_chain: List[str] = []
        self._blocked_count = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        projected_chain = self._session_chain + [tool_name]
        findings = self._analyzer.analyze(projected_chain)

        critical = [f for f in findings if f.severity == "critical"]
        high = [f for f in findings if f.severity == "high"]

        if self._block_critical and critical:
            self._blocked_count += 1
            raise EscalationBlocked(
                tool_name=tool_name,
                findings=critical,
            )
        if self._block_high and high:
            self._blocked_count += 1
            raise EscalationBlocked(
                tool_name=tool_name,
                findings=high,
            )

        result = await tool_fn(*args, **kwargs)
        self._session_chain.append(tool_name)
        return result

    def reset_session(self) -> None:
        self._session_chain = []

    def current_chain(self) -> List[str]:
        return list(self._session_chain)


class EscalationBlocked(Exception):
    def __init__(self, tool_name: str, findings: List[EscalationFinding]):
        patterns = [f.pattern for f in findings]
        super().__init__(
            f"tool '{tool_name}' blocked — escalation patterns detected: {patterns}"
        )
        self.tool_name = tool_name
        self.findings = findings
```

## Solution 5: Escalation Audit Logger

```python
import time
from typing import List


class EscalationAuditLogger:
    """
    Records escalation detections and blocks for security analysis.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        session_id: str,
        chain: List[str],
        findings: List[EscalationFinding],
        blocked: bool,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "chain_length": len(chain),
            "patterns": [f.pattern for f in findings],
            "severities": [f.severity for f in findings],
            "blocked": blocked,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        from collections import Counter
        pattern_counts = Counter(p for r in recent for p in r["patterns"])
        return {
            "window_seconds": window_seconds,
            "detections": len(recent),
            "blocked": sum(1 for r in recent if r["blocked"]),
            "top_patterns": pattern_counts.most_common(5),
        }
```

## Solution 6: Escalation Security Dashboard

```python
import time


class PrivilegeEscalationDashboard:
    """
    Combines dispatcher state and audit summary.
    """

    def __init__(
        self,
        dispatcher: EscalationGatedToolDispatcher,
        logger: EscalationAuditLogger,
    ):
        self._dispatcher = dispatcher
        self._logger = logger

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "current_session_chain": self._dispatcher.current_chain(),
            "blocked_total": self._dispatcher._blocked_count,
            "audit_summary": self._logger.summary(window_seconds),
        }
```

## Comparison

| Approach | Capability Modeling | Chain Analysis | Per-Dispatch Gate | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| ToolCapabilityProfile | Yes | No | No | No | No |
| ToolCapabilityRegistry | Via profiles | No | No | No | No |
| ToolChainEscalationAnalyzer | Via registry | Yes (3 patterns) | No | No | No |
| EscalationGatedToolDispatcher | Via registry | Via analyzer | Yes | No | No |
| EscalationAuditLogger | No | No | No | Yes | No |
| PrivilegeEscalationDashboard | No | No | No | No | Yes |

**Best for production**: Register capability profiles for every tool at startup — a tool without a profile is assigned no capabilities and allowed through, but this creates a false sense of security. Set `block_on_critical=True` unconditionally — a read-then-exfiltrate chain is never a legitimate operation pattern and should always be blocked. Use `reset_session()` at the start of each user request, not at the agent process level, so the chain analysis is scoped to a single conversation turn. Log all `EscalationBlocked` exceptions to your SIEM with the full projected chain — patterns of systematic probing (many blocked attempts with slight chain variations) indicate an active adversarial prompt injection campaign.
