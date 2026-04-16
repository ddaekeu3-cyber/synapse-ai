---
layout: solution
title: "Agent Doesn't Implement Anomaly Detection for Agent Behavior"
category: observability
description: "Continuously monitor agent behavioral metrics — response times, tool call patterns, token usage, error rates — and automatically detect anomalies that indicate degradation, prompt injection, or runaway loops."
tags: [observability, anomaly-detection, monitoring, behavioral-analysis, security, reliability]
---

# Agent Doesn't Implement Anomaly Detection for Agent Behavior

## Problem

Agent failures often manifest gradually through behavioral changes before causing outright errors: response times drift upward, tool call frequency spikes, token usage grows unexpectedly, or error rates creep above baseline. Without anomaly detection, these signals go unnoticed until users complain. Worse, prompt injection attacks and runaway loops may look like normal behavior until they cause catastrophic outcomes. Behavioral anomaly detection catches these patterns early.

## Solutions

### Option 1: Z-Score Anomaly Detection on Rolling Metrics

Compute Z-scores on rolling windows of key metrics and alert when values exceed the threshold.

```python
import anthropic
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

WINDOW_SIZE    = 20    # samples in rolling window
Z_THRESHOLD    = 2.5   # standard deviations for anomaly
MIN_SAMPLES    = 5     # minimum samples before scoring


@dataclass
class RollingMetric:
    name: str
    values: deque = field(default_factory=lambda: deque(maxlen=20))

    def record(self, value: float) -> None:
        self.values.append(value)

    def z_score(self, latest: float) -> float | None:
        if len(self.values) < MIN_SAMPLES:
            return None
        mean = statistics.mean(self.values)
        try:
            stdev = statistics.stdev(self.values)
        except statistics.StatisticsError:
            return None
        if stdev == 0:
            return 0.0
        return abs((latest - mean) / stdev)

    def is_anomalous(self, latest: float) -> tuple[bool, float | None]:
        z = self.z_score(latest)
        if z is None:
            return False, None
        return z > Z_THRESHOLD, round(z, 2)


@dataclass
class BehaviorMonitor:
    metrics: dict[str, RollingMetric] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)

    def track(self, name: str) -> RollingMetric:
        if name not in self.metrics:
            self.metrics[name] = RollingMetric(name)
        return self.metrics[name]

    def observe(self, name: str, value: float) -> bool:
        metric = self.track(name)
        anomalous, z = metric.is_anomalous(value)
        metric.record(value)
        if anomalous:
            alert = {
                "metric": name,
                "value":  round(value, 3),
                "z_score": z,
                "ts":     time.time(),
            }
            self.alerts.append(alert)
            print(f"  *** ANOMALY: {name}={value:.2f} z={z} ***")
        return anomalous

    def summary(self) -> dict:
        return {
            "total_alerts": len(self.alerts),
            "metrics": {
                name: {
                    "mean": round(statistics.mean(m.values), 3) if len(m.values) >= 2 else None,
                    "latest": m.values[-1] if m.values else None,
                }
                for name, m in self.metrics.items()
            },
        }


monitor = BehaviorMonitor()


def monitored_call(prompt: str, turn_id: int) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        latency  = (time.monotonic() - start) * 1000
        tokens   = resp.usage.input_tokens + resp.usage.output_tokens
        reply    = resp.content[0].text

        monitor.observe("latency_ms",    latency)
        monitor.observe("total_tokens",  tokens)
        monitor.observe("output_length", len(reply))
        monitor.observe("tool_calls",    0.0)

        return reply
    except Exception as e:
        monitor.observe("error_rate", 1.0)
        raise


if __name__ == "__main__":
    prompts = [
        "What is 2+2?",
        "What is 3+3?",
        "What is 4+4?",
        "What is 5+5?",
        "What is 6+6?",
        # inject an anomalous prompt that generates a long response
        "Write a detailed 500-word essay on the history of mathematics.",
        "What is 7+7?",
        "What is 8+8?",
    ]

    for i, p in enumerate(prompts):
        reply = monitored_call(p, i)
        print(f"  [{i:02d}] {p[:40]:40s} → {reply[:40]}")

    print(f"\nMonitor summary: {monitor.summary()}")

# Expected Token Savings: Early anomaly detection prevents runaway token usage before it accumulates
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Pattern-Based Tool Call Anomaly Detector

Detect anomalous tool call patterns: same tool called repeatedly, unusual argument sizes, or tool sequences never seen before.

```python
import anthropic
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

MAX_SAME_TOOL_CONSECUTIVE = 3   # alert if same tool called > N times in a row
MAX_ARG_SIZE_BYTES        = 5000
RARE_SEQUENCE_THRESHOLD   = 0.02  # alert if sequence seen < 2% of time


@dataclass
class ToolCallAnalyzer:
    call_log:       list[dict] = field(default_factory=list)
    consecutive:    dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_tool:      str = ""
    sequence_counts: Counter = field(default_factory=Counter)
    total_sequences: int = 0
    alerts:         list[dict] = field(default_factory=list)

    def _alert(self, kind: str, detail: str) -> None:
        a = {"kind": kind, "detail": detail, "ts": time.time()}
        self.alerts.append(a)
        print(f"  *** TOOL ANOMALY [{kind}]: {detail} ***")

    def analyze_call(self, tool_name: str, tool_input: dict) -> None:
        # 1. consecutive repetition check
        if tool_name == self.last_tool:
            self.consecutive[tool_name] += 1
            if self.consecutive[tool_name] >= MAX_SAME_TOOL_CONSECUTIVE:
                self._alert("repeated_tool", f"{tool_name} called {self.consecutive[tool_name]+1} times consecutively")
        else:
            # update sequence
            if self.last_tool:
                seq = (self.last_tool, tool_name)
                self.sequence_counts[seq] += 1
                self.total_sequences += 1
                freq = self.sequence_counts[seq] / self.total_sequences
                if self.total_sequences > 10 and freq < RARE_SEQUENCE_THRESHOLD:
                    self._alert("rare_sequence", f"unusual sequence {self.last_tool} → {tool_name} (freq={freq:.1%})")
            self.consecutive[tool_name] = 0
            self.last_tool = tool_name

        # 2. argument size check
        arg_size = len(json.dumps(tool_input))
        if arg_size > MAX_ARG_SIZE_BYTES:
            self._alert("large_args", f"{tool_name} args size={arg_size} > {MAX_ARG_SIZE_BYTES}")

        self.call_log.append({
            "tool":     tool_name,
            "args_len": arg_size,
            "ts":       time.time(),
        })

    def summary(self) -> dict:
        return {
            "total_calls":     len(self.call_log),
            "total_alerts":    len(self.alerts),
            "tool_frequency":  dict(Counter(c["tool"] for c in self.call_log)),
            "alerts_by_type":  dict(Counter(a["kind"] for a in self.alerts)),
        }


analyzer = ToolCallAnalyzer()

TOOLS = [
    {
        "name": "search",
        "description": "Search for information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    },
]


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    analyzer.analyze_call(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Result of {block.name}"})
            messages.append({"role": "user", "content": results})
    return "Done"


if __name__ == "__main__":
    result = agent_loop("Search for Python docs, then read the file README.md, then search again for asyncio.")
    print(f"Result: {result[:100]}")
    print(f"\nAnalyzer summary: {json.dumps(analyzer.summary(), indent=2)}")

# Expected Token Savings: Detects loops and anomalous patterns before they burn excessive tokens
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Isolation Forest–Style Behavioral Fingerprinting

Build a behavioral fingerprint per session (feature vector of key metrics) and flag sessions that deviate significantly from the normal distribution.

```python
import anthropic
import math
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Baseline: expected ranges for normal sessions
NORMAL_RANGES = {
    "tokens_per_turn":   (50,  500),
    "tool_calls_per_turn": (0,  3),
    "latency_ms":        (200, 3000),
    "turns_per_session": (1,   20),
    "error_rate":        (0.0, 0.05),
}


@dataclass
class SessionFingerprint:
    session_id: str
    turns: int = 0
    total_tokens: int = 0
    total_tool_calls: int = 0
    total_latency_ms: float = 0.0
    errors: int = 0

    def add_turn(self, tokens: int, tool_calls: int, latency_ms: float, error: bool) -> None:
        self.turns += 1
        self.total_tokens += tokens
        self.total_tool_calls += tool_calls
        self.total_latency_ms += latency_ms
        if error:
            self.errors += 1

    def to_features(self) -> dict[str, float]:
        t = max(self.turns, 1)
        return {
            "tokens_per_turn":     self.total_tokens / t,
            "tool_calls_per_turn": self.total_tool_calls / t,
            "latency_ms":          self.total_latency_ms / t,
            "turns_per_session":   float(self.turns),
            "error_rate":          self.errors / t,
        }

    def anomaly_score(self) -> float:
        """Returns 0–1; >0.7 is suspicious."""
        features = self.to_features()
        scores = []
        for name, value in features.items():
            low, high = NORMAL_RANGES[name]
            span = high - low
            if value < low:
                deviation = (low - value) / max(span, 1)
            elif value > high:
                deviation = (value - high) / max(span, 1)
            else:
                deviation = 0.0
            normalized = min(1.0, deviation)
            scores.append(normalized)
        return sum(scores) / len(scores) if scores else 0.0

    def is_anomalous(self, threshold: float = 0.5) -> bool:
        return self.anomaly_score() > threshold


def run_session(session_id: str, prompts: list[str]) -> SessionFingerprint:
    fp = SessionFingerprint(session_id=session_id)
    for prompt in prompts:
        start = time.monotonic()
        error = False
        tool_calls = 0
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            latency = (time.monotonic() - start) * 1000
            tokens  = resp.usage.input_tokens + resp.usage.output_tokens
        except Exception:
            latency = (time.monotonic() - start) * 1000
            tokens  = 0
            error   = True

        fp.add_turn(tokens, tool_calls, latency, error)

    score = fp.anomaly_score()
    status = "ANOMALOUS" if fp.is_anomalous() else "normal"
    print(f"Session {session_id}: score={score:.2f} status={status} features={fp.to_features()}")
    return fp


if __name__ == "__main__":
    # normal session
    run_session("sess_normal", [
        "What is Python?",
        "How do I read a file in Python?",
        "What is a list comprehension?",
    ])

    # anomalous session — extremely high token usage
    run_session("sess_heavy", [
        "Write a 1000-word essay on quantum mechanics." * 3,
        "Explain the entire history of computing in great detail." * 2,
    ])

    # anomalous session — too many turns
    run_session("sess_loopy", [f"Query #{i}" for i in range(25)])

# Expected Token Savings: Flag sessions for review before they exceed budget thresholds
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Real-Time Sliding Window Anomaly with Adaptive Baseline

Update the baseline continuously as new data arrives (adaptive baseline), avoiding false positives as usage patterns change over time.

```python
import anthropic
import asyncio
import time
import math
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()

WINDOW_MINUTES  = 5
ALERT_THRESHOLD = 3.0   # standard deviations
ADAPT_RATE      = 0.1   # how quickly baseline adapts (0=static, 1=instant)


@dataclass
class AdaptiveMetric:
    name: str
    window_sec: float = WINDOW_MINUTES * 60
    _samples: deque = field(default_factory=lambda: deque())
    _ema_mean: float | None = None
    _ema_var:  float | None = None
    alerts: int = 0

    def record(self, value: float, ts: float | None = None) -> bool:
        ts = ts or time.time()
        # evict old samples
        cutoff = ts - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        # update exponential moving average
        if self._ema_mean is None:
            self._ema_mean = value
            self._ema_var  = 0.0
        else:
            delta = value - self._ema_mean
            self._ema_mean += ADAPT_RATE * delta
            self._ema_var   = (1 - ADAPT_RATE) * (self._ema_var + ADAPT_RATE * delta ** 2)

        std = math.sqrt(max(self._ema_var, 1e-10))
        z   = abs((value - self._ema_mean) / std)

        self._samples.append((ts, value))

        if z > ALERT_THRESHOLD and len(self._samples) > 5:
            self.alerts += 1
            return True   # anomaly
        return False

    def stats(self) -> dict:
        return {
            "name":     self.name,
            "ema_mean": round(self._ema_mean or 0, 3),
            "ema_std":  round(math.sqrt(max(self._ema_var or 0, 0)), 3),
            "samples":  len(self._samples),
            "alerts":   self.alerts,
        }


class AdaptiveAnomalyMonitor:
    def __init__(self) -> None:
        self._metrics = {
            name: AdaptiveMetric(name)
            for name in ["latency_ms", "tokens", "output_chars", "tool_calls"]
        }
        self._alert_log: list[dict] = []

    def observe(self, name: str, value: float) -> None:
        metric = self._metrics.get(name)
        if metric and metric.record(value):
            alert = {"metric": name, "value": round(value, 2), "ts": time.time()}
            self._alert_log.append(alert)
            print(f"  *** ADAPTIVE ANOMALY: {name}={value:.1f} (baseline≈{metric._ema_mean:.1f}±{math.sqrt(metric._ema_var or 0):.1f}) ***")

    def full_stats(self) -> list[dict]:
        return [m.stats() for m in self._metrics.values()]


monitor = AdaptiveAnomalyMonitor()


async def instrumented_call(prompt: str) -> str:
    start = time.monotonic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    latency = (time.monotonic() - start) * 1000
    tokens  = resp.usage.input_tokens + resp.usage.output_tokens
    reply   = resp.content[0].text

    monitor.observe("latency_ms",    latency)
    monitor.observe("tokens",        tokens)
    monitor.observe("output_chars",  len(reply))
    monitor.observe("tool_calls",    0.0)
    return reply


async def main() -> None:
    # normal queries to establish baseline
    normal = ["What is 2+2?", "What is 3+3?", "Define 'loop'.", "What is Python?", "What is asyncio?"]
    for p in normal:
        reply = await instrumented_call(p)
        print(f"  Normal: {p[:30]:30s} → {reply[:40]}")

    # anomalous: very large output
    print("\n--- Injecting anomalous query ---")
    await instrumented_call("Write a detailed 500-word essay with examples on distributed systems.")

    print("\n--- Stats ---")
    for stat in monitor.full_stats():
        print(f"  {stat['name']:<15} mean={stat['ema_mean']:>8.1f} std={stat['ema_std']:>6.1f} alerts={stat['alerts']}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Adaptive baseline reduces false positives as usage patterns evolve
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Semantic Drift Detector for Response Quality

Detect when the agent's responses start drifting semantically — becoming shorter, more repetitive, or less coherent — indicating degraded output quality.

```python
import anthropic
import re
import statistics
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

DRIFT_WINDOW    = 10   # turns to track
DRIFT_THRESHOLD = 0.4  # relative change to flag as drift


@dataclass
class SemanticDriftDetector:
    window: int = DRIFT_WINDOW
    _length_history:   deque = field(default_factory=lambda: deque(maxlen=10))
    _vocab_history:    deque = field(default_factory=lambda: deque(maxlen=10))
    _sentence_history: deque = field(default_factory=lambda: deque(maxlen=10))
    alerts: list[dict] = field(default_factory=list)

    @staticmethod
    def _vocab_richness(text: str) -> float:
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        if not words:
            return 0.0
        return len(set(words)) / len(words)

    @staticmethod
    def _avg_sentence_length(text: str) -> float:
        sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
        if not sentences:
            return 0.0
        return statistics.mean(len(s.split()) for s in sentences)

    def _detect_drift(self, metric_name: str, history: deque, latest: float) -> bool:
        if len(history) < 5:
            return False
        baseline = statistics.mean(list(history)[:len(history)//2])
        if baseline == 0:
            return False
        change = abs(latest - baseline) / baseline
        if change > DRIFT_THRESHOLD:
            self.alerts.append({
                "metric":   metric_name,
                "baseline": round(baseline, 3),
                "latest":   round(latest, 3),
                "change":   f"{change:.1%}",
            })
            print(f"  *** DRIFT DETECTED: {metric_name} changed {change:.1%} from baseline={baseline:.2f} to {latest:.2f} ***")
            return True
        return False

    def analyze(self, response: str) -> dict:
        length  = len(response)
        vocab   = self._vocab_richness(response)
        sent_len = self._avg_sentence_length(response)

        length_drift = self._detect_drift("response_length", self._length_history, length)
        vocab_drift  = self._detect_drift("vocab_richness",  self._vocab_history,  vocab)
        sent_drift   = self._detect_drift("avg_sentence_len",self._sentence_history,sent_len)

        self._length_history.append(length)
        self._vocab_history.append(vocab)
        self._sentence_history.append(sent_len)

        return {
            "length":        length,
            "vocab_richness": round(vocab, 3),
            "avg_sent_len":  round(sent_len, 1),
            "any_drift":     length_drift or vocab_drift or sent_drift,
        }

    def summary(self) -> dict:
        return {
            "turns_analyzed": len(self._length_history),
            "total_alerts":   len(self.alerts),
            "alerts":         self.alerts,
        }


detector = SemanticDriftDetector()


def chat_with_drift_detection(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    reply = resp.content[0].text
    metrics = detector.analyze(reply)
    print(f"  len={metrics['length']:4d} vocab={metrics['vocab_richness']:.2f} sent={metrics['avg_sent_len']:.1f} drift={metrics['any_drift']}")
    return reply


if __name__ == "__main__":
    # normal prompts to establish baseline
    normal_prompts = [
        "Explain what a hash table is.",
        "What are the benefits of containerization?",
        "How does TCP/IP work?",
        "What is a REST API?",
        "Explain recursion in programming.",
    ]
    for p in normal_prompts:
        chat_with_drift_detection(p)

    # now simulate drifted responses (very short, low vocabulary)
    print("\n--- Simulating drift (short, constrained prompts) ---")
    drifted = [
        "Yes or no only: is Python popular?",
        "One word only: name a database.",
        "Single letter answer: A or B?",
    ]
    for p in drifted:
        chat_with_drift_detection(p)

    print(f"\nSummary: {detector.summary()}")

# Expected Token Savings: Drift detection flags quality issues before user abandonment
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Multi-Signal Anomaly Scorer with Severity Classification

Combine multiple anomaly signals into a weighted composite score with severity levels (INFO/WARN/CRITICAL).

```python
import anthropic
import time
import statistics
from dataclasses import dataclass, field
from collections import deque
from enum import Enum

client = anthropic.Anthropic()


class Severity(str, Enum):
    INFO     = "INFO"
    WARN     = "WARN"
    CRITICAL = "CRITICAL"


SEVERITY_THRESHOLDS = {
    "latency_ms":    {Severity.WARN: 3000,  Severity.CRITICAL: 8000},
    "total_tokens":  {Severity.WARN: 1500,  Severity.CRITICAL: 3000},
    "error_streak":  {Severity.WARN: 2,     Severity.CRITICAL: 5},
    "loop_score":    {Severity.WARN: 0.5,   Severity.CRITICAL: 0.8},
}

SIGNAL_WEIGHTS = {
    "latency_ms":   0.25,
    "total_tokens": 0.25,
    "error_streak": 0.30,
    "loop_score":   0.20,
}


@dataclass
class MultiSignalMonitor:
    _latencies:   deque = field(default_factory=lambda: deque(maxlen=20))
    _tokens:      deque = field(default_factory=lambda: deque(maxlen=20))
    _error_streak: int = 0
    _recent_prompts: deque = field(default_factory=lambda: deque(maxlen=5))
    alert_log: list[dict] = field(default_factory=list)

    def _loop_score(self, prompt: str) -> float:
        """Detect prompt repetition as a proxy for loop detection."""
        if not self._recent_prompts:
            return 0.0
        similarities = [
            len(set(prompt.lower().split()) & set(p.lower().split())) /
            max(len(set(prompt.lower().split()) | set(p.lower().split())), 1)
            for p in self._recent_prompts
        ]
        return max(similarities)

    def _score_signal(self, name: str, value: float) -> tuple[float, Severity]:
        thresholds = SEVERITY_THRESHOLDS.get(name, {})
        if value >= thresholds.get(Severity.CRITICAL, float("inf")):
            return 1.0, Severity.CRITICAL
        if value >= thresholds.get(Severity.WARN, float("inf")):
            return 0.5, Severity.WARN
        return 0.0, Severity.INFO

    def observe(self, prompt: str, latency_ms: float, tokens: int, error: bool) -> dict:
        self._latencies.append(latency_ms)
        self._tokens.append(tokens)
        if error:
            self._error_streak += 1
        else:
            self._error_streak = 0
        loop_score = self._loop_score(prompt)
        self._recent_prompts.append(prompt)

        signals = {
            "latency_ms":   latency_ms,
            "total_tokens": tokens,
            "error_streak": float(self._error_streak),
            "loop_score":   loop_score,
        }

        weighted_score = 0.0
        max_severity   = Severity.INFO
        findings       = {}

        for name, value in signals.items():
            raw_score, severity = self._score_signal(name, value)
            weighted_score += raw_score * SIGNAL_WEIGHTS.get(name, 0.25)
            findings[name] = {"value": round(value, 2), "severity": severity.value}
            if severity == Severity.CRITICAL:
                max_severity = Severity.CRITICAL
            elif severity == Severity.WARN and max_severity != Severity.CRITICAL:
                max_severity = Severity.WARN

        if max_severity != Severity.INFO:
            alert = {"score": round(weighted_score, 3), "severity": max_severity.value, "findings": findings, "ts": time.time()}
            self.alert_log.append(alert)
            print(f"  *** [{max_severity.value}] score={weighted_score:.2f} | {' | '.join(f'{k}={v[\"value\"]}' for k,v in findings.items() if v['severity'] != 'INFO')} ***")

        return {"composite_score": round(weighted_score, 3), "severity": max_severity.value, "findings": findings}


monitor = MultiSignalMonitor()


def monitored_call(prompt: str) -> str:
    start = time.monotonic()
    error = False
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = (time.monotonic() - start) * 1000
        tokens  = resp.usage.input_tokens + resp.usage.output_tokens
        result  = resp.content[0].text
    except Exception:
        latency = (time.monotonic() - start) * 1000
        tokens  = 0
        result  = ""
        error   = True

    obs = monitor.observe(prompt, latency, tokens, error)
    print(f"  [{obs['severity']:8s}] score={obs['composite_score']:.2f} | {prompt[:50]}")
    return result


if __name__ == "__main__":
    prompts = [
        "What is 2+2?",
        "What is 2+2?",   # repetition → loop score rises
        "What is 2+2?",   # even more repetition
        "Write a very long and detailed story about everything in the universe." * 3,  # token spike
        "Quick answer: yes or no?",
    ]
    for p in prompts:
        monitored_call(p)

    print(f"\nTotal alerts: {len(monitor.alert_log)}")

# Expected Token Savings: Composite scoring surfaces critical issues earlier than single-metric checks
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Signals Monitored | Adaptive Baseline | Alert Types | Complexity | Best For |
|--------|------------------|------------------|-------------|-----------|----------|
| 1 | Latency, tokens, output size | No (rolling window) | Z-score threshold | Low | General metric monitoring |
| 2 | Tool call patterns | No | Rule-based | Low | Tool-use agent security |
| 3 | Session-level features | No | Range deviation | Medium | Session-level risk scoring |
| 4 | Any metric | Yes (EMA) | Adaptive Z-score | Medium | Evolving usage patterns |
| 5 | Response semantics | No | Relative drift | Medium | Output quality degradation |
| 6 | Multi-signal composite | No | Weighted severity | Medium | Production alerting with severity |
