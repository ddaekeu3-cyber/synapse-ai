---
layout: solution
title: "Agent Doesn't Implement Error Rate Tracking by Failure Category"
category: observability
description: "Classify and count agent failures by category—rate limits, tool errors, context overflow, model refusals, timeouts—to surface actionable error patterns instead of undifferentiated failure counts."
tags: [observability, error-tracking, failure-classification, monitoring, alerting]
---

# Agent Doesn't Implement Error Rate Tracking by Failure Category

## Problem

Aggregating all failures into a single error counter makes it impossible to prioritize fixes. A 5% error rate dominated by rate limits requires a different response than one dominated by tool schema mismatches or context overflow. Without categories, dashboards are noisy and on-call responders waste time diagnosing.

## Solution Options

### Option 1: Exception-Based Error Classifier

```python
import anthropic
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ErrorTracker:
    counts: Counter = field(default_factory=Counter)
    recent: defaultdict = field(default_factory=lambda: defaultdict(list))
    MAX_RECENT = 10

    def classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "rate_limit" in msg or "429" in msg or "too many requests" in msg:
            return "rate_limit"
        elif "context_length" in msg or "max_tokens" in msg or "too long" in msg:
            return "context_overflow"
        elif "invalid_api_key" in msg or "authentication" in msg or "401" in msg:
            return "auth_failure"
        elif "timeout" in msg or "timed out" in msg or "deadline" in msg:
            return "timeout"
        elif "overloaded" in msg or "503" in msg or "529" in msg:
            return "server_overload"
        elif "invalid_request" in msg or "validation" in msg or "400" in msg:
            return "invalid_request"
        else:
            return "unknown"

    def record(self, category: str, detail: str = "") -> None:
        self.counts[category] += 1
        entry = {"ts": time.time(), "detail": detail[:100]}
        self.recent[category].append(entry)
        if len(self.recent[category]) > self.MAX_RECENT:
            self.recent[category].pop(0)

    def report(self) -> None:
        total = sum(self.counts.values())
        if not total:
            print("No errors recorded.")
            return
        print(f"\n=== Error Report (total={total}) ===")
        for cat, count in self.counts.most_common():
            pct = count / total * 100
            print(f"  {cat:<20} {count:>4} ({pct:>5.1f}%)")

tracker = ErrorTracker()

def safe_ask(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str | None:
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text
    except anthropic.RateLimitError as e:
        cat = tracker.classify_error(e)
        tracker.record(cat, str(e)[:80])
        print(f"  [ERROR:{cat}] {str(e)[:60]}")
        return None
    except anthropic.BadRequestError as e:
        cat = tracker.classify_error(e)
        tracker.record(cat, str(e)[:80])
        print(f"  [ERROR:{cat}] {str(e)[:60]}")
        return None
    except Exception as e:
        cat = tracker.classify_error(e)
        tracker.record(cat, str(e)[:80])
        print(f"  [ERROR:{cat}] {str(e)[:60]}")
        return None

prompts = [
    "What is a hash map?",
    "Explain B-trees.",
    "What is consistent hashing?",
]
for p in prompts:
    result = safe_ask(p)
    if result:
        print(f"OK: {result[:60]}...")

tracker.report()

# Expected Token Savings: N/A; reduces MTTR by surfacing actionable error categories
# Environment: production agents, on-call monitoring, error budget tracking
```

### Option 2: Structured Error Log with SQLite Persistence

```python
import anthropic
import sqlite3
import time
import uuid
from dataclasses import dataclass

client = anthropic.Anthropic()

ERROR_CATEGORIES = {
    "rate_limit":      ["rate_limit", "429", "too_many_requests"],
    "context_overflow":["context_length", "max_tokens", "too_long", "tokens"],
    "auth_failure":    ["invalid_api_key", "authentication", "401", "403"],
    "timeout":         ["timeout", "timed_out", "deadline_exceeded"],
    "server_error":    ["500", "503", "529", "overloaded", "internal_server"],
    "tool_error":      ["tool_use", "tool_call", "function_call", "invalid_tool"],
    "refusal":         ["refused", "cannot assist", "i'm not able", "against my"],
    "parse_error":     ["json", "parse", "decode", "format", "schema"],
}

def classify(text: str) -> str:
    lower = text.lower()
    for cat, keywords in ERROR_CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            return cat
    return "unknown"

def init_error_db(path: str = "/tmp/agent_errors.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS errors (
            id TEXT PRIMARY KEY,
            category TEXT,
            model TEXT,
            prompt_hash TEXT,
            error_message TEXT,
            ts REAL,
            resolved INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON errors(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON errors(ts)")
    conn.commit()
    return conn

def log_error(conn: sqlite3.Connection, category: str, model: str,
               prompt: str, error_msg: str) -> None:
    import hashlib
    conn.execute(
        "INSERT INTO errors VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4())[:8], category, model,
         hashlib.md5(prompt.encode()).hexdigest()[:8],
         error_msg[:200], time.time(), 0)
    )
    conn.commit()

def error_rate_report(conn: sqlite3.Connection, window_seconds: int = 3600) -> None:
    since = time.time() - window_seconds
    rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM errors WHERE ts > ? GROUP BY category ORDER BY cnt DESC",
        (since,)
    ).fetchall()
    total = sum(r[1] for r in rows)
    print(f"\n=== Error Rates (last {window_seconds//60}min, total={total}) ===")
    for cat, cnt in rows:
        print(f"  {cat:<20} {cnt:>4} ({cnt/max(total,1)*100:.1f}%)")

def safe_call(conn: sqlite3.Connection, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str | None:
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text
        # Check for soft refusal
        if any(phrase in text.lower() for phrase in ["i can't", "i'm not able", "i cannot assist"]):
            log_error(conn, "refusal", model, prompt, text[:100])
        return text
    except Exception as e:
        cat = classify(str(e))
        log_error(conn, cat, model, prompt, str(e))
        print(f"  [ERR:{cat}]")
        return None

conn = init_error_db()
for prompt in ["What is Redis?", "Explain Kafka.", "What is a trie?"]:
    result = safe_call(conn, prompt)
    if result:
        print(f"OK: {result[:50]}...")

error_rate_report(conn)
conn.close()

# Expected Token Savings: SQLite overhead negligible; enables trend analysis and alerting thresholds
# Environment: production monitoring, SLO dashboards, on-call runbooks
```

### Option 3: Real-Time Error Rate Window with SLO Alerting

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ErrorWindow:
    """Sliding time window error rate tracker per category."""
    window_seconds: int = 300  # 5 minute window
    events: deque = field(default_factory=deque)

    def record(self, category: str, success: bool) -> None:
        now = time.time()
        self.events.append((now, category, success))
        # Evict old events
        cutoff = now - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def error_rate(self, category: str | None = None) -> float:
        if not self.events:
            return 0.0
        if category:
            relevant = [(ts, cat, ok) for ts, cat, ok in self.events if cat == category]
        else:
            relevant = list(self.events)
        if not relevant:
            return 0.0
        errors = sum(1 for _, _, ok in relevant if not ok)
        return errors / len(relevant)

    def category_counts(self) -> dict[str, dict]:
        cats: dict[str, dict] = {}
        for _, cat, ok in self.events:
            if cat not in cats:
                cats[cat] = {"total": 0, "errors": 0}
            cats[cat]["total"] += 1
            if not ok:
                cats[cat]["errors"] += 1
        for cat in cats:
            cats[cat]["rate"] = round(cats[cat]["errors"] / max(cats[cat]["total"], 1), 3)
        return cats

@dataclass
class SLOAlert:
    category: str
    threshold: float
    fired: bool = False

    def check(self, rate: float) -> bool:
        should_alert = rate > self.threshold
        if should_alert and not self.fired:
            self.fired = True
            print(f"  [ALERT] {self.category} error rate {rate:.1%} > threshold {self.threshold:.1%}")
        elif not should_alert and self.fired:
            self.fired = False
            print(f"  [RESOLVED] {self.category} error rate normalized to {rate:.1%}")
        return should_alert

window = ErrorWindow(window_seconds=60)
slo_alerts = {
    "rate_limit": SLOAlert("rate_limit", threshold=0.10),
    "timeout": SLOAlert("timeout", threshold=0.05),
    "unknown": SLOAlert("unknown", threshold=0.02),
}

def classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in msg:
        return "timeout"
    if "context" in msg or "token" in msg:
        return "context_overflow"
    return "unknown"

def monitored_call(prompt: str) -> str | None:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        window.record("success", success=True)
        return resp.content[0].text
    except Exception as e:
        cat = classify_error(e)
        window.record(cat, success=False)
        alert = slo_alerts.get(cat)
        if alert:
            alert.check(window.error_rate(cat))
        return None

for prompt in ["Explain TCP/IP", "What is DNS?", "What is HTTPS?", "Explain TLS."]:
    result = monitored_call(prompt)
    if result:
        print(f"OK: {result[:50]}...")

# Final report
counts = window.category_counts()
print(f"\n=== 60s Window Summary ===")
for cat, stats in counts.items():
    print(f"  {cat:<20} total={stats['total']} errors={stats['errors']} rate={stats['rate']:.1%}")

# Expected Token Savings: N/A; sliding window enables rapid SLO breach detection
# Environment: production API wrappers, high-throughput agents, real-time alerting pipelines
```

### Option 4: Error Taxonomy with Automated Remediation Hints

```python
import anthropic
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ErrorCategory:
    name: str
    description: str
    remediation: str
    retry_recommended: bool
    backoff_seconds: float = 0.0

ERROR_TAXONOMY = {
    "rate_limit": ErrorCategory(
        name="rate_limit",
        description="API rate limit exceeded",
        remediation="Implement exponential backoff, reduce request frequency, or request higher quota",
        retry_recommended=True,
        backoff_seconds=60.0
    ),
    "context_overflow": ErrorCategory(
        name="context_overflow",
        description="Input exceeds model context window",
        remediation="Implement context summarization, chunking, or use a model with larger context",
        retry_recommended=False
    ),
    "auth_failure": ErrorCategory(
        name="auth_failure",
        description="Invalid or expired API credentials",
        remediation="Rotate API key, check environment variable configuration",
        retry_recommended=False
    ),
    "server_overload": ErrorCategory(
        name="server_overload",
        description="Anthropic API temporarily overloaded",
        remediation="Retry with exponential backoff, implement circuit breaker",
        retry_recommended=True,
        backoff_seconds=30.0
    ),
    "invalid_request": ErrorCategory(
        name="invalid_request",
        description="Malformed request payload",
        remediation="Validate message format, check model name, verify parameter ranges",
        retry_recommended=False
    ),
    "unknown": ErrorCategory(
        name="unknown",
        description="Unclassified error",
        remediation="Log full stack trace and error message for manual investigation",
        retry_recommended=False
    ),
}

def classify_with_taxonomy(exc: Exception) -> ErrorCategory:
    msg = str(exc).lower()
    type_name = type(exc).__name__.lower()
    if "ratelimit" in type_name or "429" in msg or "rate_limit" in msg:
        return ERROR_TAXONOMY["rate_limit"]
    if "overloaded" in msg or "529" in msg or "503" in msg:
        return ERROR_TAXONOMY["server_overload"]
    if "context" in msg or "max_tokens" in msg or "token" in msg:
        return ERROR_TAXONOMY["context_overflow"]
    if "authentication" in msg or "api_key" in msg or "401" in msg:
        return ERROR_TAXONOMY["auth_failure"]
    if "badrequest" in type_name or "invalid" in msg or "400" in msg:
        return ERROR_TAXONOMY["invalid_request"]
    return ERROR_TAXONOMY["unknown"]

@dataclass
class ErrorEvent:
    timestamp: float
    category: ErrorCategory
    exc_type: str
    message: str

error_log: list[ErrorEvent] = []

def resilient_call(prompt: str, max_retries: int = 2) -> str | None:
    for attempt in range(max_retries + 1):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.content[0].text
        except Exception as e:
            cat = classify_with_taxonomy(e)
            event = ErrorEvent(
                timestamp=time.time(),
                category=cat,
                exc_type=type(e).__name__,
                message=str(e)[:100]
            )
            error_log.append(event)
            print(f"  [Attempt {attempt+1}] {cat.name}: {cat.description}")
            print(f"    Remediation: {cat.remediation}")

            if cat.retry_recommended and attempt < max_retries:
                backoff = cat.backoff_seconds * (2 ** attempt)
                print(f"    Retrying in {backoff:.0f}s...")
                time.sleep(min(backoff, 5))
            else:
                return None
    return None

for q in ["What is eventual consistency?", "Explain CRDT data structures."]:
    result = resilient_call(q)
    if result:
        print(f"OK: {result[:80]}...")

if error_log:
    print(f"\nError log: {len(error_log)} events")
    for ev in error_log:
        print(f"  [{ev.category.name}] {ev.exc_type}: {ev.message[:60]}")

# Expected Token Savings: taxonomy-driven retry prevents wasteful retries on non-retriable errors
# Environment: production agents with SRE runbooks, automated incident response
```

### Option 5: Per-Model Error Rate Comparison

```python
import anthropic
import time
from collections import defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ModelErrorStats:
    model: str
    calls: int = 0
    errors: dict = field(default_factory=lambda: defaultdict(int))
    latencies: list = field(default_factory=list)

    def record_success(self, latency_ms: float) -> None:
        self.calls += 1
        self.latencies.append(latency_ms)

    def record_error(self, category: str) -> None:
        self.calls += 1
        self.errors[category] += 1

    @property
    def error_rate(self) -> float:
        total_errors = sum(self.errors.values())
        return total_errors / max(self.calls, 1)

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / max(len(self.latencies), 1)

    def summary(self) -> str:
        return (
            f"{self.model}: calls={self.calls} error_rate={self.error_rate:.1%} "
            f"avg_latency={self.avg_latency:.0f}ms errors={dict(self.errors)}"
        )

def classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return "rate_limit"
    if "overload" in msg or "529" in msg:
        return "overload"
    if "context" in msg or "token" in msg:
        return "context"
    return "other"

stats_by_model: dict[str, ModelErrorStats] = {}

def tracked_call(model: str, prompt: str) -> str | None:
    if model not in stats_by_model:
        stats_by_model[model] = ModelErrorStats(model=model)
    stats = stats_by_model[model]

    t0 = time.perf_counter()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        stats.record_success(round((time.perf_counter() - t0) * 1000, 2))
        return resp.content[0].text
    except Exception as e:
        stats.record_error(classify_error(e))
        return None

models = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
prompts = [
    "What is consistent hashing?",
    "Explain vector clocks.",
    "What is a Merkle tree?",
]

for model in models:
    print(f"\nTesting {model}:")
    for prompt in prompts:
        result = tracked_call(model, prompt)
        if result:
            print(f"  OK: {result[:50]}...")
        else:
            print(f"  FAIL")

print("\n=== Per-Model Error Comparison ===")
for stats in stats_by_model.values():
    print(f"  {stats.summary()}")

# Expected Token Savings: N/A; enables model reliability comparison for routing decisions
# Environment: multi-model routing, model upgrade validation, reliability benchmarks
```

### Option 6: Error Category Dashboard with Trend Detection

```python
import anthropic
import time
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CategoryTrend:
    name: str
    recent_rates: deque = field(default_factory=lambda: deque(maxlen=12))  # 12 buckets

    def add_bucket_rate(self, rate: float) -> None:
        self.recent_rates.append(rate)

    def is_trending_up(self) -> bool:
        if len(self.recent_rates) < 3:
            return False
        recent = list(self.recent_rates)
        first_half_avg = statistics.mean(recent[:len(recent)//2])
        second_half_avg = statistics.mean(recent[len(recent)//2:])
        return second_half_avg > first_half_avg * 1.5

    def latest_rate(self) -> float:
        return self.recent_rates[-1] if self.recent_rates else 0.0

class ErrorDashboard:
    BUCKET_SECONDS = 30

    def __init__(self):
        self.current_bucket_start = time.time()
        self.current_bucket: defaultdict = defaultdict(lambda: {"total": 0, "errors": 0})
        self.trends: dict[str, CategoryTrend] = {}
        self.all_time_counts: defaultdict = defaultdict(int)

    def _flush_bucket(self) -> None:
        for cat, counts in self.current_bucket.items():
            rate = counts["errors"] / max(counts["total"], 1)
            if cat not in self.trends:
                self.trends[cat] = CategoryTrend(name=cat)
            self.trends[cat].add_bucket_rate(rate)
        self.current_bucket = defaultdict(lambda: {"total": 0, "errors": 0})
        self.current_bucket_start = time.time()

    def record(self, category: str, success: bool) -> None:
        if time.time() - self.current_bucket_start > self.BUCKET_SECONDS:
            self._flush_bucket()
        self.current_bucket[category]["total"] += 1
        if not success:
            self.current_bucket[category]["errors"] += 1
            self.all_time_counts[category] += 1

    def print_dashboard(self) -> None:
        self._flush_bucket()
        print("\n=== Error Category Dashboard ===")
        if not self.trends:
            print("  No data yet.")
            return
        for cat, trend in sorted(self.trends.items(), key=lambda x: x[1].latest_rate(), reverse=True):
            arrow = "↑ TRENDING UP" if trend.is_trending_up() else ""
            rates = [f"{r:.1%}" for r in list(trend.recent_rates)[-3:]]
            print(f"  {cat:<20} latest={trend.latest_rate():.1%} recent=[{', '.join(rates)}] {arrow}")
        print(f"\n  All-time error counts: {dict(self.all_time_counts)}")

dashboard = ErrorDashboard()

def classify_exc(exc: Exception) -> str:
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return "rate_limit"
    if "overload" in msg:
        return "overload"
    if "context" in msg:
        return "context"
    return "other"

def dashboard_call(prompt: str) -> str | None:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        dashboard.record("success", success=True)
        return resp.content[0].text
    except Exception as e:
        cat = classify_exc(e)
        dashboard.record(cat, success=False)
        return None

questions = [
    "What is a queue?", "What is a stack?", "What is a heap?",
    "What is a graph?", "What is a tree?", "What is a trie?"
]
for q in questions:
    result = dashboard_call(q)
    if result:
        print(f"OK: {result[:50]}...")

dashboard.print_dashboard()

# Expected Token Savings: N/A; trend detection enables proactive alerting before SLO breach
# Environment: production dashboards, error budget monitoring, proactive capacity management
```

## Comparison

| Option | Storage | Trend Detection | Remediation Hints | Best For |
|--------|---------|-----------------|-------------------|----------|
| 1 | In-memory Counter | No | No | Quick instrumentation |
| 2 | SQLite | No | No | Persistent audit log |
| 3 | Sliding window deque | No | No | Real-time SLO alerting |
| 4 | In-memory list | No | Yes | Automated remediation |
| 5 | Per-model stats | No | No | Model reliability comparison |
| 6 | Bucket aggregation | Yes | No | Trend dashboards |
