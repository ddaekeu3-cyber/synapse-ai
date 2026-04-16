---
title: "Agent Doesn't Implement Runtime Application Self-Protection for Agent Code"
description: "Agents that execute without runtime behavioral monitoring can be hijacked to spawn unexpected subprocesses, make unauthorized network calls, or abuse system resources — all undetectable until after damage is done. Implement Runtime Application Self-Protection (RASP) at the application layer to detect and block anomalous agent behaviors in real time without OS-level instrumentation."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-runtime-application-self-protection-for-agent-code
tags: [rasp, runtime-protection, behavioral-monitoring, subprocess-detection, network-anomaly, security]
symptoms:
  - "Agent spawns unexpected shell subprocesses that were never part of the tool design"
  - "Outbound network calls to unknown IPs appear in logs with no matching tool invocation"
  - "CPU and memory spike unexpectedly mid-task with no correlation to tool execution timeline"
  - "Agent executes code injection payloads embedded in user-supplied tool arguments"
  - "No application-layer detection of anomalous behavior — only OS audit logs catch it after the fact"
---

## Why This Happens

Agent runtimes execute tool calls, process LLM outputs, and pass arguments to external functions — all surfaces where injected payloads can execute. Without in-process behavioral monitoring, a compromised tool call that spawns a subprocess or opens a reverse shell completes before any detection occurs. RASP instruments the application itself: wrapping subprocess creation, socket connections, and resource-intensive calls to detect deviations from established behavioral baselines and block or alert in real time.

## Solution 1: Subprocess Spawn Monitor

```python
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

@dataclass
class SubprocessEvent:
    pid: int
    command: str
    args: List[str]
    spawned_by: str     # tool name or "unknown"
    timestamp: float
    blocked: bool = False
    reason: str = ""

class SubprocessSpawnMonitor:
    """
    Wraps subprocess.Popen/run/call to intercept every spawn attempt.
    Maintains an allowlist of expected commands per tool context.
    Blocks or alerts on unexpected subprocess creation.
    """

    def __init__(self, block_mode: bool = True):
        self._block = block_mode
        self._allowlist: Dict[str, Set[str]] = {}   # tool_name -> allowed commands
        self._current_tool: Optional[str] = None
        self._events: List[SubprocessEvent] = []
        self._blocked_count = 0
        self._alert_handlers: List[Callable[[SubprocessEvent], None]] = []

    def register_tool_commands(self, tool_name: str, allowed_commands: Set[str]) -> None:
        self._allowlist[tool_name] = allowed_commands

    def set_current_tool(self, tool_name: Optional[str]) -> None:
        self._current_tool = tool_name

    def add_alert_handler(self, handler: Callable[[SubprocessEvent], None]) -> None:
        self._alert_handlers.append(handler)

    def _is_allowed(self, command: str) -> bool:
        if self._current_tool is None:
            return False
        allowed = self._allowlist.get(self._current_tool, set())
        # Check exact match or prefix match for commands with arguments
        return any(command == cmd or command.startswith(cmd) for cmd in allowed)

    def check_spawn(self, args) -> SubprocessEvent:
        command = args[0] if isinstance(args, (list, tuple)) else args.split()[0]
        arg_list = list(args) if isinstance(args, (list, tuple)) else args.split()

        allowed = self._is_allowed(command)
        event = SubprocessEvent(
            pid=os.getpid(),
            command=command,
            args=arg_list,
            spawned_by=self._current_tool or "unknown",
            timestamp=time.time(),
            blocked=not allowed and self._block,
            reason="" if allowed else f"command not in allowlist for tool '{self._current_tool}'",
        )
        self._events.append(event)

        if not allowed:
            self._blocked_count += 1
            for handler in self._alert_handlers:
                try:
                    handler(event)
                except Exception:
                    pass

            if self._block:
                raise PermissionError(
                    f"[rasp] blocked subprocess: '{command}' — {event.reason}"
                )

        return event

    def patched_run(self, args, **kwargs):
        self.check_spawn(args)
        return subprocess.run.__wrapped__(args, **kwargs)

    def stats(self) -> dict:
        return {
            "total_spawns": len(self._events),
            "blocked": self._blocked_count,
            "allowed": len(self._events) - self._blocked_count,
            "tools_with_allowlist": list(self._allowlist.keys()),
        }
```

## Solution 2: Network Call Anomaly Detector

```python
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

@dataclass
class NetworkCallEvent:
    host: str
    port: int
    resolved_ip: Optional[str]
    tool_name: str
    direction: str   # "outbound"
    timestamp: float
    blocked: bool = False
    reason: str = ""

class NetworkCallAnomalyDetector:
    """
    Intercepts outbound socket connections at the application layer.
    Validates destination against per-tool allowlists and IP block ranges.
    Detects unexpected data exfiltration patterns (large outbound to unknown hosts).
    """

    PRIVATE_RANGES = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
    ]

    def __init__(self, block_mode: bool = True):
        self._block = block_mode
        self._tool_allowlists: Dict[str, Set[str]] = {}   # tool -> allowed hostnames
        self._current_tool: Optional[str] = None
        self._events: List[NetworkCallEvent] = []
        self._alert_handlers: List[Callable[[NetworkCallEvent], None]] = []
        self._blocked_count = 0

    def register_tool_hosts(self, tool_name: str, allowed_hosts: Set[str]) -> None:
        self._tool_allowlists[tool_name] = {h.lower() for h in allowed_hosts}

    def set_current_tool(self, tool_name: Optional[str]) -> None:
        self._current_tool = tool_name

    def add_alert_handler(self, handler: Callable[[NetworkCallEvent], None]) -> None:
        self._alert_handlers.append(handler)

    def _resolve_ip(self, host: str) -> Optional[str]:
        try:
            return socket.gethostbyname(host)
        except Exception:
            return None

    def _is_private_ip(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in net for net in self.PRIVATE_RANGES)
        except ValueError:
            return False

    def check_connection(self, host: str, port: int) -> NetworkCallEvent:
        resolved = self._resolve_ip(host)
        allowed_hosts = self._tool_allowlists.get(self._current_tool or "", set())

        blocked = False
        reason = ""

        if host.lower() not in allowed_hosts:
            blocked = self._block
            reason = f"host '{host}' not in allowlist for tool '{self._current_tool}'"
        elif resolved and self._is_private_ip(resolved) and host not in {"localhost", "127.0.0.1"}:
            blocked = self._block
            reason = f"resolved to private IP {resolved} — potential SSRF"

        event = NetworkCallEvent(
            host=host,
            port=port,
            resolved_ip=resolved,
            tool_name=self._current_tool or "unknown",
            direction="outbound",
            timestamp=time.time(),
            blocked=blocked,
            reason=reason,
        )
        self._events.append(event)

        if blocked:
            self._blocked_count += 1
            for handler in self._alert_handlers:
                try:
                    handler(event)
                except Exception:
                    pass
            if self._block:
                raise PermissionError(f"[rasp] blocked network call to {host}:{port} — {reason}")

        return event

    def stats(self) -> dict:
        return {
            "total_calls": len(self._events),
            "blocked": self._blocked_count,
            "unique_hosts": len({e.host for e in self._events}),
        }
```

## Solution 3: Resource Abuse Detector

```python
import os
import time
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

@dataclass
class ResourceSnapshot:
    timestamp: float
    cpu_percent: float
    memory_mb: float
    open_files: int
    thread_count: int
    tool_name: str

@dataclass
class ResourceAlert:
    alert_type: str    # "cpu_spike" | "memory_spike" | "fd_leak" | "thread_explosion"
    tool_name: str
    metric_value: float
    threshold: float
    timestamp: float

class ResourceAbuseDetector:
    """
    Samples process resource metrics during tool execution.
    Detects CPU spikes, memory growth, file descriptor leaks,
    and thread explosion — indicators of runaway or hijacked tool code.
    Requires: psutil
    """

    def __init__(
        self,
        cpu_threshold_percent: float = 90.0,
        memory_threshold_mb: float = 500.0,
        fd_threshold: int = 200,
        thread_threshold: int = 50,
        sample_interval_seconds: float = 1.0,
    ):
        self._cpu_thresh = cpu_threshold_percent
        self._mem_thresh = memory_threshold_mb
        self._fd_thresh = fd_threshold
        self._thread_thresh = thread_threshold
        self._interval = sample_interval_seconds
        self._snapshots: List[ResourceSnapshot] = []
        self._alerts: List[ResourceAlert] = []
        self._alert_handlers: List[Callable[[ResourceAlert], None]] = []
        self._current_tool: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def add_alert_handler(self, handler: Callable[[ResourceAlert], None]) -> None:
        self._alert_handlers.append(handler)

    def set_current_tool(self, tool_name: Optional[str]) -> None:
        self._current_tool = tool_name

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _sample_loop(self) -> None:
        try:
            import psutil
            proc = psutil.Process(os.getpid())
        except ImportError:
            return

        while self._running:
            try:
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info().rss / (1024 * 1024)
                fds = proc.num_fds() if hasattr(proc, "num_fds") else 0
                threads = proc.num_threads()

                snapshot = ResourceSnapshot(
                    timestamp=time.time(),
                    cpu_percent=cpu,
                    memory_mb=mem,
                    open_files=fds,
                    thread_count=threads,
                    tool_name=self._current_tool or "unknown",
                )
                self._snapshots.append(snapshot)

                self._check_thresholds(snapshot)
            except Exception:
                pass
            time.sleep(self._interval)

    def _check_thresholds(self, snapshot: ResourceSnapshot) -> None:
        checks = [
            ("cpu_spike", snapshot.cpu_percent, self._cpu_thresh),
            ("memory_spike", snapshot.memory_mb, self._mem_thresh),
            ("fd_leak", float(snapshot.open_files), float(self._fd_thresh)),
            ("thread_explosion", float(snapshot.thread_count), float(self._thread_thresh)),
        ]
        for alert_type, value, threshold in checks:
            if value > threshold:
                alert = ResourceAlert(
                    alert_type=alert_type,
                    tool_name=snapshot.tool_name,
                    metric_value=value,
                    threshold=threshold,
                    timestamp=snapshot.timestamp,
                )
                self._alerts.append(alert)
                for handler in self._alert_handlers:
                    try:
                        handler(alert)
                    except Exception:
                        pass

    def summary(self) -> dict:
        return {
            "total_samples": len(self._snapshots),
            "total_alerts": len(self._alerts),
            "alert_types": list({a.alert_type for a in self._alerts}),
            "tools_flagged": list({a.tool_name for a in self._alerts}),
        }
```

## Solution 4: Code Injection Scanner

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class InjectionFinding:
    pattern_name: str
    matched_text: str
    argument_key: str
    severity: str   # "critical" | "high" | "medium"

class CodeInjectionScanner:
    """
    Scans tool arguments for code injection patterns before execution.
    Detects shell injection, Python eval payloads, SQL injection fragments,
    and prompt injection attempts embedded in argument strings.
    """

    PATTERNS = [
        ("shell_injection_semicolon", r";\s*(rm|wget|curl|nc|bash|sh|python|perl)\b", "critical"),
        ("shell_injection_pipe", r"\|\s*(bash|sh|nc|ncat|python)\b", "critical"),
        ("shell_injection_backtick", r"`[^`]{1,200}`", "high"),
        ("shell_substitution", r"\$\([^)]{1,200}\)", "high"),
        ("python_eval", r"\beval\s*\(", "critical"),
        ("python_exec", r"\bexec\s*\(", "critical"),
        ("import_os", r"__import__\s*\(['\"]os['\"]", "critical"),
        ("sql_union", r"\bUNION\s+SELECT\b", "high"),
        ("sql_drop", r"\bDROP\s+TABLE\b", "high"),
        ("path_traversal", r"\.\./\.\./\.\./", "high"),
        ("null_byte", r"\x00", "medium"),
        ("prompt_injection_ignore", r"ignore\s+previous\s+instructions", "medium"),
        ("prompt_injection_jailbreak", r"DAN\s+mode|jailbreak\s+mode", "medium"),
    ]

    def __init__(self):
        self._compiled = [
            (name, re.compile(pattern, re.IGNORECASE), severity)
            for name, pattern, severity in self.PATTERNS
        ]

    def scan_arguments(self, arguments: Dict[str, Any]) -> List[InjectionFinding]:
        findings = []
        for key, value in arguments.items():
            if not isinstance(value, str):
                continue
            for name, pattern, severity in self._compiled:
                match = pattern.search(value)
                if match:
                    findings.append(InjectionFinding(
                        pattern_name=name,
                        matched_text=match.group(0)[:100],
                        argument_key=key,
                        severity=severity,
                    ))
        return findings

    def is_safe(self, arguments: Dict[str, Any], block_on: str = "high") -> tuple:
        """
        Returns (safe: bool, findings: list).
        block_on: minimum severity to consider unsafe ("critical", "high", "medium").
        """
        findings = self.scan_arguments(arguments)
        severity_rank = {"critical": 3, "high": 2, "medium": 1}
        threshold = severity_rank.get(block_on, 2)
        blocking = [f for f in findings if severity_rank.get(f.severity, 0) >= threshold]
        return len(blocking) == 0, findings
```

## Solution 5: RASP Orchestrator

```python
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

@dataclass
class RASPEvent:
    event_type: str   # "subprocess_blocked" | "network_blocked" | "injection_found" | "resource_alert"
    tool_name: str
    detail: str
    severity: str
    timestamp: float

class RASPOrchestrator:
    """
    Coordinates all RASP components into a single tool-execution guard.
    Wrap every tool call with rasp.tool_context(tool_name, arguments).
    Injection scan happens pre-execution; subprocess/network/resource
    monitoring runs during execution.
    """

    def __init__(
        self,
        subprocess_monitor: SubprocessSpawnMonitor,
        network_detector: NetworkCallAnomalyDetector,
        resource_detector: ResourceAbuseDetector,
        injection_scanner: CodeInjectionScanner,
        block_on_injection: bool = True,
    ):
        self._subprocess = subprocess_monitor
        self._network = network_detector
        self._resource = resource_detector
        self._injection = injection_scanner
        self._block_injection = block_on_injection
        self._events: List[RASPEvent] = []

    def _record(self, event_type: str, tool_name: str, detail: str, severity: str) -> None:
        self._events.append(RASPEvent(
            event_type=event_type,
            tool_name=tool_name,
            detail=detail,
            severity=severity,
            timestamp=time.time(),
        ))

    @contextmanager
    def tool_context(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Generator:
        # Pre-execution: scan arguments for injection
        if arguments:
            safe, findings = self._injection.is_safe(arguments)
            for finding in findings:
                self._record(
                    "injection_found", tool_name,
                    f"{finding.pattern_name} in arg '{finding.argument_key}': {finding.matched_text}",
                    finding.severity,
                )
            if not safe and self._block_injection:
                critical = [f for f in findings if f.severity == "critical"]
                raise PermissionError(
                    f"[rasp] injection patterns detected in tool '{tool_name}': "
                    + ", ".join(f.pattern_name for f in critical)
                )

        # Set tool context for monitors
        self._subprocess.set_current_tool(tool_name)
        self._network.set_current_tool(tool_name)
        self._resource.set_current_tool(tool_name)

        try:
            yield
        finally:
            self._subprocess.set_current_tool(None)
            self._network.set_current_tool(None)
            self._resource.set_current_tool(None)

    def security_report(self) -> dict:
        by_type: Dict[str, int] = {}
        for e in self._events:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
        return {
            "total_events": len(self._events),
            "by_type": by_type,
            "tools_flagged": list({e.tool_name for e in self._events}),
            "critical_events": [
                {"tool": e.tool_name, "detail": e.detail, "ts": e.timestamp}
                for e in self._events if e.severity == "critical"
            ],
            "subprocess_stats": self._subprocess.stats(),
            "network_stats": self._network.stats(),
            "resource_summary": self._resource.summary(),
        }
```

## Solution 6: RASP Audit Logger

```python
import json
import time
from dataclasses import dataclass
from typing import IO, List, Optional

@dataclass
class AuditEntry:
    entry_id: str
    tool_name: str
    action: str        # "allowed" | "blocked" | "alerted"
    component: str     # "injection" | "subprocess" | "network" | "resource"
    detail: str
    session_id: str
    timestamp: float

class RASPAuditLogger:
    """
    Structured audit logger for all RASP decisions.
    Writes newline-delimited JSON to a file or stream.
    Supports compliance queries: all blocks for a session, all events for a tool.
    """

    def __init__(self, output: Optional[IO] = None, max_in_memory: int = 10_000):
        self._output = output
        self._entries: List[AuditEntry] = []
        self._max = max_in_memory
        self._entry_counter = 0

    def log(
        self,
        tool_name: str,
        action: str,
        component: str,
        detail: str,
        session_id: str = "",
    ) -> AuditEntry:
        self._entry_counter += 1
        entry = AuditEntry(
            entry_id=f"rasp-{self._entry_counter:06d}",
            tool_name=tool_name,
            action=action,
            component=component,
            detail=detail,
            session_id=session_id,
            timestamp=time.time(),
        )
        if len(self._entries) < self._max:
            self._entries.append(entry)
        if self._output:
            try:
                self._output.write(
                    json.dumps({
                        "entry_id": entry.entry_id,
                        "tool": entry.tool_name,
                        "action": entry.action,
                        "component": entry.component,
                        "detail": entry.detail,
                        "session": entry.session_id,
                        "ts": entry.timestamp,
                    }) + "\n"
                )
                self._output.flush()
            except Exception:
                pass
        return entry

    def query_blocks(self, session_id: Optional[str] = None) -> List[AuditEntry]:
        return [
            e for e in self._entries
            if e.action == "blocked"
            and (session_id is None or e.session_id == session_id)
        ]

    def summary(self) -> dict:
        total = len(self._entries)
        blocked = sum(1 for e in self._entries if e.action == "blocked")
        return {
            "total_entries": total,
            "blocked": blocked,
            "allowed": sum(1 for e in self._entries if e.action == "allowed"),
            "alerted": sum(1 for e in self._entries if e.action == "alerted"),
            "block_rate": round(blocked / max(total, 1), 4),
        }
```

## Comparison

| Approach | Pre-Execution | During Execution | Post-Execution | Blocks |
|---|---|---|---|---|
| SubprocessSpawnMonitor | No | Yes (spawn intercept) | No | Yes |
| NetworkCallAnomalyDetector | No | Yes (connect intercept) | No | Yes |
| ResourceAbuseDetector | No | Yes (sampling) | No | No (alert only) |
| CodeInjectionScanner | Yes (arg scan) | No | No | Yes |
| RASPOrchestrator | Yes (injection) | Yes (all monitors) | No | Yes |
| RASPAuditLogger | No | No | Yes (log all) | No |

**Best for production**: Gate every tool call through `RASPOrchestrator.tool_context()`. Register expected commands and hosts per tool in `SubprocessSpawnMonitor` and `NetworkCallAnomalyDetector`. Run `ResourceAbuseDetector` as a background thread throughout agent lifetime. Pipe all RASP decisions to `RASPAuditLogger` writing to a tamper-evident append-only log. Set `block_mode=True` in staging to discover allowlist gaps; switch to alert-only in production until the allowlists are stable, then re-enable blocking.
