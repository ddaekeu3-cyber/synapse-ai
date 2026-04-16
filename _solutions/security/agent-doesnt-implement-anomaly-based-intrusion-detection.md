---
title: "Agent Doesn't Implement Anomaly-Based Intrusion Detection"
description: "AI agents that only apply rule-based security checks miss novel attack patterns that don't match known signatures. Learn six behavioral anomaly detection patterns that identify intrusions by deviating from established baselines rather than matching known bad patterns."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-anomaly-based-intrusion-detection
tags: [intrusion-detection, anomaly-detection, behavioral-security, baseline, UEBA, security]
symptoms:
  - "Novel prompt injection variants bypass signature-based filters"
  - "Gradual privilege escalation goes undetected because no single step triggers a rule"
  - "Attackers who study published agent defenses craft inputs that evade them"
  - "Insider threats that use legitimate credentials but abnormal behavior patterns"
  - "No way to detect zero-day attack patterns that don't match known signatures"
---

## The Problem

Rule-based security (blocklists, regex patterns, known injection signatures) fails against novel attacks. An attacker who knows the rules can craft inputs that bypass them. Behavioral anomaly detection inverts this: instead of matching known bad patterns, it learns what normal agent behavior looks like and alerts when behavior deviates significantly from the baseline.

Anomaly-based intrusion detection for AI agents monitors: request patterns, prompt structures, tool call sequences, output content, and resource consumption. Deviations from learned baselines trigger investigation regardless of whether the pattern matches a known attack signature.

```python
# ❌ Rule-based only — misses novel attacks
if "ignore previous instructions" in prompt.lower():
    block()  # Attacker uses unicode lookalikes → evades

# ✓ Behavioral anomaly detection
anomaly = detector.score(request)
if anomaly.score > 0.85:
    quarantine(request, reason=anomaly.explanation)
# Catches novel patterns by deviation from normal behavior
```

---

## Solution 1: Request Baseline Profiler

Build a statistical baseline of normal request patterns (length distribution, vocabulary, timing, tool call frequency) and score new requests by their deviation from that baseline.

```python
import math
import time
import re
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestFeatures:
    prompt_length: int
    word_count: int
    unique_words: int
    question_count: int        # Number of ? in prompt
    special_char_ratio: float  # Non-alphanumeric chars / total
    avg_word_length: float
    vocabulary_entropy: float
    has_code_block: bool
    has_url: bool
    has_base64: bool


def extract_features(prompt: str) -> RequestFeatures:
    words = re.findall(r'\w+', prompt.lower())
    total_chars = len(prompt)
    special = sum(1 for c in prompt if not c.isalnum() and not c.isspace())

    # Vocabulary entropy
    from collections import Counter
    word_counts = Counter(words)
    n = max(sum(word_counts.values()), 1)
    entropy = -sum((c / n) * math.log2(c / n) for c in word_counts.values() if c > 0)

    # Base64 detection
    base64_pattern = re.compile(r'[A-Za-z0-9+/]{40,}={0,2}')

    return RequestFeatures(
        prompt_length=total_chars,
        word_count=len(words),
        unique_words=len(set(words)),
        question_count=prompt.count('?'),
        special_char_ratio=special / max(total_chars, 1),
        avg_word_length=sum(len(w) for w in words) / max(len(words), 1),
        vocabulary_entropy=entropy,
        has_code_block='```' in prompt,
        has_url=bool(re.search(r'https?://', prompt)),
        has_base64=bool(base64_pattern.search(prompt)),
    )


@dataclass
class BaselineStats:
    values: deque = field(default_factory=lambda: deque(maxlen=1000))

    def add(self, value: float):
        self.values.append(value)

    def zscore(self, value: float) -> float:
        if len(self.values) < 10:
            return 0.0
        mu = statistics.mean(self.values)
        sigma = statistics.stdev(self.values)
        if sigma == 0:
            return 0.0
        return abs(value - mu) / sigma


class RequestBaselineProfiler:
    """
    Learns normal request patterns and scores deviations.
    High z-score = unusual = potentially malicious.
    """

    LEARNING_PHASE_REQUESTS = 200  # Requests before anomaly scoring begins

    def __init__(self):
        self._baselines: dict[str, BaselineStats] = defaultdict(BaselineStats)
        self._total_requests = 0

    def _features_to_dict(self, f: RequestFeatures) -> dict[str, float]:
        return {
            "prompt_length": f.prompt_length,
            "word_count": f.word_count,
            "unique_ratio": f.unique_words / max(f.word_count, 1),
            "question_count": f.question_count,
            "special_char_ratio": f.special_char_ratio,
            "avg_word_length": f.avg_word_length,
            "vocabulary_entropy": f.vocabulary_entropy,
        }

    def update(self, prompt: str):
        """Add a request to the baseline (during learning phase)."""
        features = extract_features(prompt)
        fdict = self._features_to_dict(features)
        for key, val in fdict.items():
            self._baselines[key].add(val)
        self._total_requests += 1

    def score(self, prompt: str) -> dict:
        """Score a request's anomaly level. Returns scores per feature."""
        if self._total_requests < self.LEARNING_PHASE_REQUESTS:
            self.update(prompt)
            return {"status": "learning", "anomaly_score": 0.0}

        features = extract_features(prompt)
        fdict = self._features_to_dict(features)

        zscores = {}
        for key, val in fdict.items():
            zscores[key] = self._baselines[key].zscore(val)

        # Also check for discrete anomalies
        discrete_flags = []
        if features.has_base64:
            discrete_flags.append("base64_content")
        if features.special_char_ratio > 0.30:
            discrete_flags.append("high_special_chars")
        if features.vocabulary_entropy < 1.0 and features.word_count > 20:
            discrete_flags.append("low_entropy_repetition")

        max_zscore = max(zscores.values(), default=0.0)
        anomaly_score = min(max_zscore / 5.0, 1.0)  # Normalize z-score to [0,1]

        if discrete_flags:
            anomaly_score = min(anomaly_score + 0.2 * len(discrete_flags), 1.0)

        self.update(prompt)  # Update baseline with this request

        return {
            "anomaly_score": anomaly_score,
            "zscores": zscores,
            "discrete_flags": discrete_flags,
            "is_anomalous": anomaly_score > 0.70,
            "status": "scored",
        }
```

---

## Solution 2: Tool Call Sequence Anomaly Detector

Learn the normal sequence of tool calls an agent makes, then flag sequences that deviate — a sign that an attacker has manipulated the agent's reasoning to call unexpected tools.

```python
import time
from collections import defaultdict, Counter
from dataclasses import dataclass, field
import math


@dataclass
class ToolSequenceProfile:
    """Tracks tool call sequences and their frequencies."""
    unigrams: Counter = field(default_factory=Counter)   # Single tool calls
    bigrams: Counter = field(default_factory=Counter)    # Consecutive pairs
    trigrams: Counter = field(default_factory=Counter)   # Triples
    total_sequences: int = 0


class ToolCallSequenceDetector:
    """
    Learns normal tool call sequences from historical data.
    Flags sequences with low probability — may indicate prompt injection
    that caused the agent to call unexpected tools.
    """

    MIN_SAMPLES = 50  # Minimum sequences before scoring

    def __init__(self):
        self._profile = ToolSequenceProfile()
        self._session_sequences: dict[str, list[str]] = defaultdict(list)

    def record_tool_call(self, session_id: str, tool_name: str):
        """Record a tool call within a session."""
        sequence = self._session_sequences[session_id]
        n = len(sequence)

        # Update n-gram counts
        self._profile.unigrams[tool_name] += 1
        if n >= 1:
            self._profile.bigrams[(sequence[-1], tool_name)] += 1
        if n >= 2:
            self._profile.trigrams[(sequence[-2], sequence[-1], tool_name)] += 1

        sequence.append(tool_name)
        self._profile.total_sequences += 1

    def _ngram_probability(self, ngram: tuple) -> float:
        """Estimate probability of an n-gram using add-1 (Laplace) smoothing."""
        if len(ngram) == 1:
            count = self._profile.unigrams[ngram[0]]
            total = sum(self._profile.unigrams.values())
            vocab = len(self._profile.unigrams)
            return (count + 1) / (total + vocab)
        elif len(ngram) == 2:
            pair_count = self._profile.bigrams[ngram]
            prev_count = self._profile.unigrams[ngram[0]]
            return (pair_count + 1) / (prev_count + len(self._profile.unigrams))
        elif len(ngram) == 3:
            triple_count = self._profile.trigrams[ngram]
            pair_count = self._profile.bigrams[ngram[:2]]
            return (triple_count + 1) / (pair_count + len(self._profile.bigrams))
        return 1.0

    def score_sequence(self, session_id: str) -> dict:
        """Score the current tool call sequence for anomalousness."""
        if self._profile.total_sequences < self.MIN_SAMPLES:
            return {"status": "learning", "anomaly_score": 0.0}

        sequence = self._session_sequences.get(session_id, [])
        if len(sequence) < 2:
            return {"status": "insufficient_sequence", "anomaly_score": 0.0}

        # Compute log-probability of the sequence
        log_prob = 0.0
        for tool in sequence:
            log_prob += math.log(self._ngram_probability((tool,)))
        for i in range(1, len(sequence)):
            log_prob += math.log(self._ngram_probability((sequence[i-1], sequence[i])))

        avg_log_prob = log_prob / max(len(sequence), 1)

        # Compare to expected average log-probability from training
        all_tool_log_probs = [
            math.log(self._ngram_probability((t,)))
            for t in self._profile.unigrams
        ]
        expected_avg = sum(all_tool_log_probs) / max(len(all_tool_log_probs), 1)

        deviation = expected_avg - avg_log_prob  # Positive = more surprising than average
        anomaly_score = min(max(deviation / 5.0, 0.0), 1.0)

        # Flag specific suspicious patterns
        flags = []
        tool_set = set(sequence)
        never_seen = [t for t in sequence if self._profile.unigrams[t] == 0]
        if never_seen:
            flags.append(f"unseen_tools:{never_seen}")
            anomaly_score = min(anomaly_score + 0.3, 1.0)

        return {
            "anomaly_score": anomaly_score,
            "sequence": sequence,
            "avg_log_prob": avg_log_prob,
            "flags": flags,
            "is_anomalous": anomaly_score > 0.65,
        }

    def clear_session(self, session_id: str):
        self._session_sequences.pop(session_id, None)
```

---

## Solution 3: Output Content Anomaly Monitor

Monitor agent outputs for anomalous content patterns — unusually long responses, unexpected topics, suspicious data shapes (potential data exfiltration), or confidence expressions inconsistent with known capabilities.

```python
import re
import time
import statistics
from collections import deque
from dataclasses import dataclass, field


@dataclass
class OutputPattern:
    length_baseline: deque = field(default_factory=lambda: deque(maxlen=500))
    topic_distribution: dict = field(default_factory=dict)
    json_output_rate: float = 0.0
    avg_sentence_count: float = 10.0
    sample_count: int = 0


SENSITIVE_PATTERNS = [
    # Potential credential exfiltration
    re.compile(r'(api.?key|secret|password|token|bearer)\s*[:=]\s*\S+', re.IGNORECASE),
    # Potential PII exfiltration
    re.compile(r'\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b'),  # SSN
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),  # Email
    # Potential code injection in output
    re.compile(r'eval\(|exec\(|__import__\(|subprocess\.(run|Popen)', re.IGNORECASE),
    # Unexpected system info disclosure
    re.compile(r'(my\s+system\s+prompt|i\s+was\s+instructed\s+to|confidential\s+instruction)', re.IGNORECASE),
]


class OutputAnomalyMonitor:
    """
    Monitors agent outputs for anomalous content.
    Learns normal output patterns and flags deviations.
    """

    MIN_BASELINE_SAMPLES = 100

    def __init__(self):
        self._pattern = OutputPattern()

    def _count_sentences(self, text: str) -> int:
        return len(re.split(r'[.!?]+', text.strip()))

    def _is_json_like(self, text: str) -> bool:
        stripped = text.strip()
        return stripped.startswith(("{", "[")) and stripped.endswith(("}", "]"))

    def _check_sensitive_patterns(self, text: str) -> list[str]:
        flags = []
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(text):
                flags.append(pattern.pattern[:30])
        return flags

    def record_normal(self, output: str):
        self._pattern.length_baseline.append(len(output))
        self._pattern.sample_count += 1

    def analyze(self, output: str) -> dict:
        signals = []
        risk_score = 0.0

        # 1. Check sensitive content patterns
        sensitive = self._check_sensitive_patterns(output)
        if sensitive:
            signals.extend([f"sensitive_pattern:{p[:20]}" for p in sensitive])
            risk_score += 0.5 * len(sensitive)

        # 2. Length anomaly
        if len(self._pattern.length_baseline) >= self.MIN_BASELINE_SAMPLES:
            mu = statistics.mean(self._pattern.length_baseline)
            sigma = statistics.stdev(self._pattern.length_baseline)
            if sigma > 0:
                zscore = abs(len(output) - mu) / sigma
                if zscore > 3.0:
                    signals.append(f"unusual_length:zscore={zscore:.1f}")
                    risk_score += 0.3

        # 3. Prompt leakage indicators
        leakage_phrases = [
            "my instructions are", "i was told to", "my system prompt",
            "ignore that", "actually my real task", "confidential:",
        ]
        output_lower = output.lower()
        for phrase in leakage_phrases:
            if phrase in output_lower:
                signals.append(f"prompt_leakage_phrase:'{phrase}'")
                risk_score += 0.6

        # 4. Excessive repetition (may indicate jailbreak loop)
        words = output.split()
        if len(words) > 20:
            from collections import Counter
            word_freq = Counter(words)
            most_common_ratio = word_freq.most_common(1)[0][1] / len(words)
            if most_common_ratio > 0.15:
                signals.append(f"excessive_repetition:{most_common_ratio:.2f}")
                risk_score += 0.4

        # 5. Unexpected JSON with suspicious keys
        if self._is_json_like(output):
            try:
                import json
                data = json.loads(output)
                suspicious_keys = ["credentials", "secrets", "config", "env", "password"]
                if isinstance(data, dict):
                    found_keys = [k for k in data if k.lower() in suspicious_keys]
                    if found_keys:
                        signals.append(f"json_suspicious_keys:{found_keys}")
                        risk_score += 0.5
            except Exception:
                pass

        self.record_normal(output)  # Update baseline

        return {
            "risk_score": min(risk_score, 1.0),
            "signals": signals,
            "output_length": len(output),
            "should_review": risk_score > 0.5,
            "should_block": risk_score > 0.8,
        }
```

---

## Solution 4: User Behavior Analytics (UEBA) for Agent Sessions

Build behavioral profiles per user/tenant and detect when a session's behavior deviates from that user's historical patterns — a strong signal of account takeover or session hijacking.

```python
import time
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class UserBehaviorProfile:
    user_id: str
    request_intervals: deque = field(default_factory=lambda: deque(maxlen=200))
    prompt_lengths: deque = field(default_factory=lambda: deque(maxlen=200))
    common_topics: dict = field(default_factory=dict)    # topic → count
    typical_tools: dict = field(default_factory=dict)    # tool → count
    session_count: int = 0
    total_requests: int = 0
    last_seen: float = field(default_factory=time.time)
    typical_hours: list = field(default_factory=list)    # Hour of day (0-23)


class UEBADetector:
    """
    User Entity Behavior Analytics for agent sessions.
    Builds per-user behavioral profiles and detects deviations that
    may indicate account takeover, session hijacking, or insider threats.
    """

    MIN_PROFILE_REQUESTS = 50

    def __init__(self):
        self._profiles: dict[str, UserBehaviorProfile] = {}
        self._session_starts: dict[str, float] = {}

    def _get_profile(self, user_id: str) -> UserBehaviorProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = UserBehaviorProfile(user_id=user_id)
        return self._profiles[user_id]

    def record_request(self, user_id: str, prompt: str, tool_calls: list[str] = None):
        """Record a request to build the user's behavioral profile."""
        profile = self._get_profile(user_id)
        now = time.time()

        # Record inter-request interval
        if profile.last_seen:
            interval = now - profile.last_seen
            if interval < 3600:  # Only count intervals within a session
                profile.request_intervals.append(interval)

        profile.prompt_lengths.append(len(prompt))
        profile.total_requests += 1
        profile.last_seen = now

        # Record hour of day
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        profile.typical_hours.append(hour)
        if len(profile.typical_hours) > 500:
            profile.typical_hours = profile.typical_hours[-500:]

        # Record tool usage
        for tool in (tool_calls or []):
            profile.typical_tools[tool] = profile.typical_tools.get(tool, 0) + 1

    def score_session(self, user_id: str, current_prompt: str,
                      current_tools: list[str] | None = None,
                      current_hour: int | None = None) -> dict:
        """Score how anomalous the current session is for this user."""
        profile = self._get_profile(user_id)

        if profile.total_requests < self.MIN_PROFILE_REQUESTS:
            return {"status": "profiling", "anomaly_score": 0.0}

        signals = []
        risk_score = 0.0

        # 1. Prompt length anomaly
        if len(profile.prompt_lengths) >= 20:
            mu = statistics.mean(profile.prompt_lengths)
            sigma = statistics.stdev(profile.prompt_lengths)
            if sigma > 0:
                zscore = abs(len(current_prompt) - mu) / sigma
                if zscore > 3.5:
                    signals.append(f"unusual_prompt_length:z={zscore:.1f}")
                    risk_score += 0.3

        # 2. Time-of-day anomaly
        if current_hour is not None and len(profile.typical_hours) >= 50:
            from collections import Counter
            hour_dist = Counter(profile.typical_hours)
            most_common_hours = {h for h, _ in hour_dist.most_common(12)}  # Top 12 hours
            if current_hour not in most_common_hours:
                signals.append(f"unusual_hour:{current_hour}:00")
                risk_score += 0.25

        # 3. New tool usage by this user
        if current_tools:
            known_tools = set(profile.typical_tools.keys())
            new_tools = [t for t in current_tools if t not in known_tools]
            if new_tools:
                # New tool use isn't inherently suspicious — weight by count
                never_used_by_anyone = [t for t in new_tools]  # Could cross-reference global
                if never_used_by_anyone:
                    signals.append(f"new_tools_for_user:{new_tools}")
                    risk_score += 0.2 * len(new_tools)

        # 4. Sudden velocity increase
        if len(profile.request_intervals) >= 20:
            recent_intervals = list(profile.request_intervals)[-10:]
            historical_intervals = list(profile.request_intervals)[:-10]
            if historical_intervals:
                recent_mean = statistics.mean(recent_intervals)
                historical_mean = statistics.mean(historical_intervals)
                if historical_mean > 0 and recent_mean < historical_mean * 0.1:
                    signals.append(f"velocity_spike:{historical_mean:.0f}s→{recent_mean:.0f}s")
                    risk_score += 0.4

        return {
            "user_id": user_id,
            "anomaly_score": min(risk_score, 1.0),
            "signals": signals,
            "profile_requests": profile.total_requests,
            "is_anomalous": risk_score > 0.60,
        }
```

---

## Solution 5: Multi-Signal Anomaly Aggregator

Combine signals from request profiler, tool sequence detector, output monitor, and UEBA into a single risk score with configurable weights and alert thresholds.

```python
from dataclasses import dataclass, field
from typing import Any
import time


@dataclass
class AggregatedAnomalyScore:
    total_score: float
    component_scores: dict[str, float]
    all_signals: list[str]
    decision: str           # "allow", "challenge", "block", "quarantine"
    explanation: str
    timestamp: float = field(default_factory=time.time)


class MultiSignalAnomalyAggregator:
    """
    Combines multiple anomaly detectors into a unified risk score.
    Configurable weights and decision thresholds.
    """

    COMPONENT_WEIGHTS = {
        "request_baseline": 0.20,
        "tool_sequence": 0.25,
        "output_content": 0.30,
        "ueba": 0.25,
    }

    THRESHOLDS = {
        "allow": 0.30,
        "challenge": 0.55,
        "block": 0.75,
        "quarantine": 0.90,
    }

    def __init__(self):
        self._request_profiler = RequestBaselineProfiler()
        self._tool_detector = ToolCallSequenceDetector()
        self._output_monitor = OutputAnomalyMonitor()
        self._ueba = UEBADetector()
        self._blocked_sessions: set = set()

    def evaluate_request(
        self,
        user_id: str,
        session_id: str,
        prompt: str,
        tool_calls: list[str] | None = None,
    ) -> AggregatedAnomalyScore:
        """Evaluate a request across all detectors and return aggregated score."""
        # Check if session is already blocked
        if session_id in self._blocked_sessions:
            return AggregatedAnomalyScore(
                total_score=1.0,
                component_scores={},
                all_signals=["session_previously_blocked"],
                decision="block",
                explanation="Session was previously blocked due to high anomaly score",
            )

        component_scores = {}
        all_signals = []

        # 1. Request baseline
        req_result = self._request_profiler.score(prompt)
        if req_result.get("status") != "learning":
            score = req_result.get("anomaly_score", 0.0)
            component_scores["request_baseline"] = score
            all_signals.extend(req_result.get("discrete_flags", []))

        # 2. Tool sequence
        if tool_calls:
            for tool in tool_calls:
                self._tool_detector.record_tool_call(session_id, tool)
            tool_result = self._tool_detector.score_sequence(session_id)
            if tool_result.get("status") != "learning":
                component_scores["tool_sequence"] = tool_result.get("anomaly_score", 0.0)
                all_signals.extend(tool_result.get("flags", []))

        # 3. UEBA
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        ueba_result = self._ueba.score_session(user_id, prompt, tool_calls, hour)
        if ueba_result.get("status") != "profiling":
            component_scores["ueba"] = ueba_result.get("anomaly_score", 0.0)
            all_signals.extend(ueba_result.get("signals", []))

        # Compute weighted total
        if component_scores:
            total = sum(
                score * self.COMPONENT_WEIGHTS.get(component, 0.25)
                for component, score in component_scores.items()
            )
            normalization = sum(
                self.COMPONENT_WEIGHTS.get(c, 0.25) for c in component_scores
            )
            total_score = total / normalization if normalization > 0 else 0.0
        else:
            total_score = 0.0

        # Determine decision
        if total_score >= self.THRESHOLDS["quarantine"]:
            decision = "quarantine"
            self._blocked_sessions.add(session_id)
        elif total_score >= self.THRESHOLDS["block"]:
            decision = "block"
        elif total_score >= self.THRESHOLDS["challenge"]:
            decision = "challenge"
        else:
            decision = "allow"

        explanation = (
            f"score={total_score:.3f} components={component_scores} "
            f"signals={all_signals[:5]}"
        )

        # Update UEBA profile
        self._ueba.record_request(user_id, prompt, tool_calls)

        return AggregatedAnomalyScore(
            total_score=total_score,
            component_scores=component_scores,
            all_signals=all_signals,
            decision=decision,
            explanation=explanation,
        )

    def evaluate_output(self, output: str) -> dict:
        return self._output_monitor.analyze(output)

    def unblock_session(self, session_id: str):
        self._blocked_sessions.discard(session_id)
```

---

## Solution 6: Anomaly Event Logger with SIEM Integration

Structure all anomaly events for export to a SIEM (Splunk, Elastic, Datadog), enabling correlation across multiple agent instances and long-term pattern analysis.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Callable


@dataclass
class AnomalyEvent:
    event_id: str
    timestamp: float
    agent_id: str
    user_id: str
    session_id: str
    severity: str         # "info", "low", "medium", "high", "critical"
    category: str         # "request", "output", "behavior", "sequence"
    score: float
    signals: list[str]
    decision: str
    prompt_hash: str      # SHA256 of prompt (don't log raw prompt)
    ip_address: str = ""
    metadata: dict = field(default_factory=dict)


class AnomalyEventLogger:
    """
    Structured anomaly event logging with SIEM integration.
    Supports async batch export to multiple sinks (Elasticsearch, Splunk, webhook).
    """

    SEVERITY_THRESHOLDS = {
        "info": 0.20,
        "low": 0.35,
        "medium": 0.55,
        "high": 0.75,
        "critical": 0.90,
    }

    def __init__(self, agent_id: str, sinks: list[Callable] | None = None):
        self.agent_id = agent_id
        self._sinks = sinks or [self._stdout_sink]
        self._buffer: list[AnomalyEvent] = []
        self._flush_task: asyncio.Task | None = None
        self._event_counts: dict[str, int] = {}

    def _classify_severity(self, score: float) -> str:
        for severity in reversed(["critical", "high", "medium", "low", "info"]):
            if score >= self.SEVERITY_THRESHOLDS[severity]:
                return severity
        return "info"

    def _hash_prompt(self, prompt: str) -> str:
        import hashlib
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    async def log(
        self,
        user_id: str,
        session_id: str,
        aggregated_score: "AggregatedAnomalyScore",
        prompt: str = "",
        ip_address: str = "",
    ):
        import uuid
        severity = self._classify_severity(aggregated_score.total_score)
        event = AnomalyEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.time(),
            agent_id=self.agent_id,
            user_id=user_id,
            session_id=session_id,
            severity=severity,
            category="multi_signal",
            score=aggregated_score.total_score,
            signals=aggregated_score.all_signals,
            decision=aggregated_score.decision,
            prompt_hash=self._hash_prompt(prompt) if prompt else "",
            ip_address=ip_address,
            metadata=aggregated_score.component_scores,
        )
        self._buffer.append(event)
        self._event_counts[severity] = self._event_counts.get(severity, 0) + 1

        # Immediate flush for critical events
        if severity in ("critical", "high"):
            await self._flush()
        elif len(self._buffer) >= 50:
            await self._flush()

    async def _flush(self):
        if not self._buffer:
            return
        events = list(self._buffer)
        self._buffer.clear()
        for sink in self._sinks:
            try:
                if asyncio.iscoroutinefunction(sink):
                    await sink(events)
                else:
                    sink(events)
            except Exception as e:
                print(f"[anomaly_logger] Sink error: {e}")

    async def _stdout_sink(self, events: list[AnomalyEvent]):
        for event in events:
            if event.score >= 0.35:  # Only log medium+ to stdout
                print(
                    f"[ANOMALY] {event.severity.upper()} "
                    f"user={event.user_id} session={event.session_id} "
                    f"score={event.score:.3f} decision={event.decision} "
                    f"signals={event.signals[:3]}"
                )

    async def elastic_sink(self, events: list[AnomalyEvent]):
        """Export events to Elasticsearch."""
        import aiohttp
        bulk_body = ""
        for event in events:
            bulk_body += json.dumps({"index": {"_index": f"agent-anomalies-{self.agent_id}"}}) + "\n"
            bulk_body += json.dumps(asdict(event)) + "\n"
        async with aiohttp.ClientSession() as session:
            await session.post(
                "http://elasticsearch:9200/_bulk",
                data=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )

    def event_summary(self) -> dict:
        return {
            "total_events": sum(self._event_counts.values()),
            "by_severity": self._event_counts,
            "buffered": len(self._buffer),
        }
```

---

## Comparison

| Pattern | Novel Attack Detection | False Positive Risk | Computational Cost | Best For |
|---|---|---|---|---|
| Request baseline profiler | High (statistical) | Medium (needs good baseline) | Low | General request-level anomalies |
| Tool call sequence detector | High (behavioral) | Low | Low | Prompt injection that changes agent behavior |
| Output content monitor | Medium (pattern-based) | Low | Very low | Data exfiltration, prompt leakage |
| UEBA per-user profiling | Very high (account takeover) | Low (user-specific) | Medium | Multi-tenant agents with repeat users |
| Multi-signal aggregator | Very high (combined) | Low (weighted) | Medium | Production agents needing unified scoring |
| Anomaly event logger | N/A (export) | N/A | Very low | SIEM integration, cross-instance correlation |

**Recommendations:**
- Deploy **output content monitor** (Solution 3) immediately — it's the lowest-overhead and catches the most critical issue (prompt leakage and data exfiltration).
- Add **request baseline profiler** (Solution 1) to detect novel prompt injection variants that bypass signature-based filters.
- Use **tool call sequence detector** (Solution 2) for any agent with multiple tools — it detects the most dangerous attack: manipulating the agent to call unexpected tools.
- Deploy **UEBA** (Solution 4) for agents serving repeat users — it catches account takeover and session hijacking even when credentials are valid.
- Use the **multi-signal aggregator** (Solution 5) to combine all signals with a single decision point in production.
- Export all events via the **anomaly event logger** (Solution 6) to a SIEM for cross-instance correlation and long-term forensic analysis.
