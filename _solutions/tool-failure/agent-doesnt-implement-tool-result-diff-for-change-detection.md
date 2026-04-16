---
title: "Agent Doesn't Implement Tool Result Diff for Change Detection"
description: "Agents that call the same tool repeatedly — polling a file, checking an API, monitoring state — waste tokens and processing on identical results. Tool result diffing detects what actually changed between calls, letting agents skip unchanged results and focus only on meaningful differences."
difficulty: intermediate
category: tool-failure
tags: [tool-use, diffing, change-detection, polling, caching, efficiency, state-monitoring]
---

## Problem

An agent monitoring a file, database record, or API response calls the same tool every few seconds. Without change detection, it processes the full result every time — sending unchanged data to the model, triggering unnecessary reasoning, and burning tokens on "nothing changed." Tool result diffing compares the new result against the last known state and only proceeds when something actually changed.

```python
# BAD: process every result even when nothing changed
async def monitor_file(path: str, interval: int = 10):
    while True:
        content = await read_file(path)
        await process_with_model(content)  # always called, always full content
        await asyncio.sleep(interval)
# Sends identical content to model 6 times/minute
```

## Solution 1: Hash-Based Change Detection

Compare MD5/SHA256 hashes to detect any change before processing.

```python
import asyncio
import hashlib
import time
from pathlib import Path
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ToolResultState:
    content_hash: str
    content: str
    last_checked: float
    last_changed: float
    check_count: int = 0
    change_count: int = 0

def content_hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]

class HashBasedChangeDetector:
    def __init__(self):
        self._states: dict[str, ToolResultState] = {}

    def check(self, tool_key: str, result: str) -> tuple[bool, str | None]:
        """
        Returns (changed, diff_summary).
        changed=True means new content; diff_summary describes what changed.
        """
        new_hash = content_hash(result)
        now = time.time()

        if tool_key not in self._states:
            self._states[tool_key] = ToolResultState(
                content_hash=new_hash,
                content=result,
                last_checked=now,
                last_changed=now,
            )
            return True, "Initial result"

        state = self._states[tool_key]
        state.check_count += 1
        state.last_checked = now

        if new_hash == state.content_hash:
            return False, None  # no change

        # Changed
        old_len = len(state.content)
        new_len = len(result)
        diff_summary = f"Content changed: {old_len} → {new_len} chars (hash: {state.content_hash} → {new_hash})"

        state.content_hash = new_hash
        state.content = result
        state.last_changed = now
        state.change_count += 1

        return True, diff_summary

    def stats(self, tool_key: str) -> dict | None:
        state = self._states.get(tool_key)
        if not state:
            return None
        return {
            "checks": state.check_count,
            "changes": state.change_count,
            "change_rate": state.change_count / max(state.check_count, 1),
            "last_changed_ago": round(time.time() - state.last_changed, 1),
        }

# Simulated tool
_file_versions = ["version 1", "version 1", "version 2", "version 2", "version 3"]
_call_counter = 0

async def read_monitored_file(path: str) -> str:
    global _call_counter
    result = _file_versions[min(_call_counter, len(_file_versions) - 1)]
    _call_counter += 1
    return result

async def process_change(content: str, diff_summary: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"File changed ({diff_summary}). New content: {content}\n\nSummarize the change in one sentence."
        }]
    )
    return response.content[0].text if response.content else ""

async def monitored_polling_loop(path: str, poll_count: int = 5):
    detector = HashBasedChangeDetector()
    processed = 0
    skipped = 0

    for i in range(poll_count):
        content = await read_monitored_file(path)
        changed, diff = detector.check(path, content)

        if changed:
            print(f"[Poll {i+1}] CHANGED: {diff}")
            summary = await process_change(content, diff or "initial")
            print(f"  → {summary[:100]}")
            processed += 1
        else:
            print(f"[Poll {i+1}] No change — skipping model call")
            skipped += 1

    print(f"\nStats: {processed} processed, {skipped} skipped")
    print(f"Tool stats: {detector.stats(path)}")

asyncio.run(monitored_polling_loop("/tmp/monitored.txt", poll_count=5))
```

## Solution 2: Structural Diff for JSON Tool Results

Compare JSON structures field-by-field to identify exactly what changed.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Any

client = AsyncAnthropic()

@dataclass
class FieldDiff:
    path: str
    old_value: Any
    new_value: Any
    change_type: str  # "added" | "removed" | "modified"

def json_diff(old: dict | list, new: dict | list, path: str = "") -> list[FieldDiff]:
    """Recursively diff two JSON structures."""
    diffs: list[FieldDiff] = []

    if type(old) != type(new):
        return [FieldDiff(path, old, new, "modified")]

    if isinstance(old, dict):
        all_keys = set(old.keys()) | set(new.keys())
        for key in all_keys:
            child_path = f"{path}.{key}" if path else key
            if key not in old:
                diffs.append(FieldDiff(child_path, None, new[key], "added"))
            elif key not in new:
                diffs.append(FieldDiff(child_path, old[key], None, "removed"))
            elif old[key] != new[key]:
                if isinstance(old[key], (dict, list)):
                    diffs.extend(json_diff(old[key], new[key], child_path))
                else:
                    diffs.append(FieldDiff(child_path, old[key], new[key], "modified"))

    elif isinstance(old, list):
        if old != new:
            diffs.append(FieldDiff(path, old, new, "modified"))

    elif old != new:
        diffs.append(FieldDiff(path, old, new, "modified"))

    return diffs

def format_diffs(diffs: list[FieldDiff]) -> str:
    if not diffs:
        return "No changes"
    parts = []
    for d in diffs[:10]:  # cap at 10 to avoid token bloat
        if d.change_type == "added":
            parts.append(f"+ {d.path}: {d.new_value!r}")
        elif d.change_type == "removed":
            parts.append(f"- {d.path}: {d.old_value!r}")
        else:
            parts.append(f"~ {d.path}: {d.old_value!r} → {d.new_value!r}")
    if len(diffs) > 10:
        parts.append(f"... and {len(diffs) - 10} more changes")
    return "\n".join(parts)

class JsonDiffMonitor:
    def __init__(self):
        self._last_results: dict[str, dict | list] = {}

    def check(self, tool_key: str, result: dict | list) -> tuple[list[FieldDiff], bool]:
        if tool_key not in self._last_results:
            self._last_results[tool_key] = result
            return [], True  # first result

        diffs = json_diff(self._last_results[tool_key], result)
        if diffs:
            self._last_results[tool_key] = result
        return diffs, bool(diffs)

# Simulated API responses
API_RESPONSES = [
    {"status": "healthy", "users_online": 142, "error_count": 0, "version": "1.2.3"},
    {"status": "healthy", "users_online": 156, "error_count": 0, "version": "1.2.3"},
    {"status": "degraded", "users_online": 89, "error_count": 47, "version": "1.2.3"},
    {"status": "degraded", "users_online": 89, "error_count": 47, "version": "1.2.3"},
    {"status": "healthy", "users_online": 201, "error_count": 0, "version": "1.2.4"},
]

async def process_json_change(diffs: list[FieldDiff], new_state: dict) -> str:
    diff_text = format_diffs(diffs)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"API state changes:\n{diff_text}\n\nCurrent state: {json.dumps(new_state)}\n\nOne-sentence assessment:"
        }]
    )
    return response.content[0].text if response.content else ""

async def monitor_api(poll_count: int = 5):
    monitor = JsonDiffMonitor()
    for i, response in enumerate(API_RESPONSES[:poll_count]):
        diffs, changed = monitor.check("api_status", response)
        if changed:
            print(f"\n[Poll {i+1}] Changes detected ({len(diffs)} fields):")
            print(format_diffs(diffs))
            assessment = await process_api_change(diffs, response) if i > 0 else "Initial state captured"
            print(f"→ {assessment[:100]}")
        else:
            print(f"[Poll {i+1}] No change")

async def process_api_change(diffs, state):
    return await process_json_change(diffs, state)

asyncio.run(monitor_api(5))
```

## Solution 3: Line-Level Text Diff

For text-based tool results, compute a unified diff and send only changed lines to the model.

```python
import asyncio
import difflib
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def compute_line_diff(old: str, new: str, context_lines: int = 2) -> tuple[str, int, int]:
    """
    Returns (unified_diff, lines_added, lines_removed).
    Returns empty string if no changes.
    """
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="previous", tofile="current",
        n=context_lines
    ))

    if not diff:
        return "", 0, 0

    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return "".join(diff), added, removed

class TextDiffMonitor:
    def __init__(self, significance_threshold: int = 3):
        """Only trigger on diffs with >= threshold changed lines."""
        self._last_texts: dict[str, str] = {}
        self._threshold = significance_threshold

    def check(self, key: str, new_text: str) -> tuple[str, bool, dict]:
        if key not in self._last_texts:
            self._last_texts[key] = new_text
            return "", True, {"status": "initial"}

        diff, added, removed = compute_line_diff(self._last_texts[key], new_text)

        if not diff:
            return "", False, {"status": "unchanged"}

        significant = (added + removed) >= self._threshold
        if significant:
            self._last_texts[key] = new_text

        return diff, significant, {
            "status": "changed" if significant else "minor_change",
            "lines_added": added,
            "lines_removed": removed,
            "significant": significant
        }

# Simulated log file content at different poll times
LOG_SNAPSHOTS = [
    "INFO 10:00 Server started\nINFO 10:00 Listening on port 8080",
    "INFO 10:00 Server started\nINFO 10:00 Listening on port 8080\nINFO 10:01 Request received",
    "INFO 10:00 Server started\nINFO 10:00 Listening on port 8080\nINFO 10:01 Request received",
    "INFO 10:00 Server started\nINFO 10:00 Listening on port 8080\nINFO 10:01 Request received\nERROR 10:05 Connection refused\nERROR 10:05 Retrying...",
    "INFO 10:00 Server started\nINFO 10:00 Listening on port 8080\nINFO 10:01 Request received\nERROR 10:05 Connection refused\nERROR 10:05 Retrying...\nINFO 10:06 Connection restored",
]

async def analyze_log_change(diff: str, stats: dict) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"Log diff (+{stats['lines_added']} lines, -{stats['lines_removed']} lines):\n{diff[:500]}\n\nOne-sentence summary of what changed:"
        }]
    )
    return response.content[0].text if response.content else ""

async def monitor_log_file(polls: int = 5):
    monitor = TextDiffMonitor(significance_threshold=1)
    for i, snapshot in enumerate(LOG_SNAPSHOTS[:polls]):
        diff, changed, stats = monitor.check("server.log", snapshot)
        if changed:
            analysis = await analyze_log_change(diff, stats)
            print(f"[Poll {i+1}] {stats['status']}: +{stats['lines_added']}/-{stats['lines_removed']} lines")
            print(f"  → {analysis[:120]}")
        else:
            print(f"[Poll {i+1}] {stats.get('status', 'unchanged')} — skipped")

asyncio.run(monitor_log_file(5))
```

## Solution 4: Semantic Change Detection

Use the model to determine if a change is semantically significant, not just textually different.

```python
import asyncio
import hashlib
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SEMANTIC_JUDGE_PROMPT = """You are a change significance classifier. Given a previous state and new state, determine if the change is semantically significant enough to act on.

Respond with JSON only:
{
  "significant": true/false,
  "reason": "brief explanation",
  "change_type": "critical" | "notable" | "cosmetic" | "noise"
}

critical: requires immediate action (errors, outages, security)
notable: worth logging or reporting (status changes, new data)
cosmetic: formatting or minor text (ignore)
noise: random variation within normal range (ignore)"""

async def is_semantically_significant(old_content: str, new_content: str, context: str = "") -> dict:
    prompt = (
        f"Context: {context}\n\n"
        f"Previous:\n{old_content[:400]}\n\n"
        f"Current:\n{new_content[:400]}"
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SEMANTIC_JUDGE_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"significant": True, "reason": "parse error", "change_type": "notable"}

class SemanticDiffMonitor:
    def __init__(self, context: str = ""):
        self._last_contents: dict[str, str] = {}
        self._context = context
        self._semantic_checks = 0
        self._significant_count = 0

    async def check(self, key: str, new_content: str) -> tuple[bool, dict]:
        if key not in self._last_contents:
            self._last_contents[key] = new_content
            return True, {"significant": True, "reason": "initial", "change_type": "notable"}

        old = self._last_contents[key]

        # Quick hash check first — skip semantic check if identical
        old_hash = hashlib.md5(old.encode()).hexdigest()
        new_hash = hashlib.md5(new_content.encode()).hexdigest()
        if old_hash == new_hash:
            return False, {"significant": False, "reason": "identical", "change_type": "noise"}

        # Semantic check for changed content
        self._semantic_checks += 1
        verdict = await is_semantically_significant(old, new_content, self._context)

        if verdict.get("significant", True):
            self._last_contents[key] = new_content
            self._significant_count += 1

        return verdict.get("significant", True), verdict

# Test data
MONITORING_DATA = [
    "Server healthy. Response time: 45ms. Requests/sec: 120.",
    "Server healthy. Response time: 47ms. Requests/sec: 118.",  # cosmetic — noise
    "Server DEGRADED. Response time: 2100ms. Requests/sec: 12. DB connection pool exhausted.",  # critical
    "Server DEGRADED. Response time: 2100ms. Requests/sec: 12. DB connection pool exhausted.",  # identical
    "Server recovering. Response time: 380ms. Requests/sec: 67. DB pool restored.",  # notable
]

async def semantic_monitoring_loop():
    monitor = SemanticDiffMonitor(context="Production API server monitoring")
    for i, data in enumerate(MONITORING_DATA):
        significant, verdict = await monitor.check("api_status", data)
        change_type = verdict.get("change_type", "unknown")
        reason = verdict.get("reason", "")

        if significant:
            print(f"[Poll {i+1}] SIGNIFICANT ({change_type}): {reason}")
            print(f"  Content: {data[:80]}")
        else:
            print(f"[Poll {i+1}] Skip ({change_type}): {reason}")

    print(f"\nSemantic checks: {monitor._semantic_checks}, Significant: {monitor._significant_count}")

asyncio.run(semantic_monitoring_loop())
```

## Solution 5: Windowed Change Aggregation

Accumulate small changes over a time window and process them as a batch.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ChangeEvent:
    key: str
    old_value: str
    new_value: str
    detected_at: float = field(default_factory=time.time)

class WindowedChangeAggregator:
    def __init__(self, window_seconds: float = 5.0, max_batch: int = 10):
        self._window = window_seconds
        self._max_batch = max_batch
        self._last_values: dict[str, str] = {}
        self._pending: list[ChangeEvent] = []
        self._window_start: float = time.time()

    def record(self, key: str, new_value: str) -> bool:
        """Returns True if change was detected."""
        old = self._last_values.get(key, "")
        if old == new_value:
            return False
        self._last_values[key] = new_value
        self._pending.append(ChangeEvent(key, old, new_value))
        return True

    def should_flush(self) -> bool:
        if not self._pending:
            return False
        window_elapsed = time.time() - self._window_start > self._window
        batch_full = len(self._pending) >= self._max_batch
        return window_elapsed or batch_full

    def flush(self) -> list[ChangeEvent]:
        events = list(self._pending)
        self._pending.clear()
        self._window_start = time.time()
        return events

async def process_change_batch(events: list[ChangeEvent]) -> str:
    if not events:
        return ""
    changes_text = "\n".join(
        f"- {e.key}: '{e.old_value[:50]}' → '{e.new_value[:50]}'"
        for e in events
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Summarize these {len(events)} state changes:\n{changes_text}"
        }]
    )
    return response.content[0].text if response.content else ""

async def windowed_monitor(metrics_stream: list[dict], window_seconds: float = 2.0):
    aggregator = WindowedChangeAggregator(window_seconds=window_seconds)
    processed_batches = 0

    for i, metrics in enumerate(metrics_stream):
        for key, value in metrics.items():
            aggregator.record(key, str(value))

        if aggregator.should_flush():
            events = aggregator.flush()
            if events:
                print(f"\n[Batch {processed_batches+1}] Processing {len(events)} changes:")
                summary = await process_change_batch(events)
                print(f"  {summary[:150]}")
                processed_batches += 1
        else:
            print(f"[Sample {i+1}] Buffering {len(aggregator._pending)} pending changes")
        await asyncio.sleep(0.5)

    # Final flush
    events = aggregator.flush()
    if events:
        print(f"\n[Final Batch] {len(events)} changes")
        summary = await process_change_batch(events)
        print(f"  {summary[:150]}")

# Simulate metric stream
METRIC_STREAM = [
    {"cpu": "45%", "memory": "62%", "status": "ok"},
    {"cpu": "47%", "memory": "62%", "status": "ok"},
    {"cpu": "89%", "memory": "78%", "status": "ok"},
    {"cpu": "91%", "memory": "85%", "status": "warning"},
    {"cpu": "91%", "memory": "85%", "status": "warning"},
    {"cpu": "95%", "memory": "92%", "status": "critical"},
]

asyncio.run(windowed_monitor(METRIC_STREAM, window_seconds=1.0))
```

## Solution 6: Fingerprint-Based Deduplication with Version History

Maintain a version history and deduplicate repeated states efficiently.

```python
import asyncio
import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class Version:
    fingerprint: str
    content: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    seen_count: int = 1

class FingerprintVersionTracker:
    def __init__(self, history_size: int = 20):
        self._history: deque[Version] = deque(maxlen=history_size)
        self._current_fp: str | None = None
        self._fingerprints: dict[str, Version] = {}

    def _fingerprint(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:12]

    def record(self, content: str) -> tuple[bool, Version, Version | None]:
        """
        Returns (is_new, current_version, previous_version).
        is_new=True means this fingerprint hasn't been seen recently.
        """
        fp = self._fingerprint(content)

        if fp == self._current_fp and fp in self._fingerprints:
            v = self._fingerprints[fp]
            v.seen_count += 1
            v.last_seen = time.time()
            return False, v, None

        prev_version = self._fingerprints.get(self._current_fp) if self._current_fp else None

        if fp in self._fingerprints:
            # Seen before but not current — state reverted
            v = self._fingerprints[fp]
            v.seen_count += 1
            v.last_seen = time.time()
            self._current_fp = fp
            return True, v, prev_version  # reversion is still "new"

        # Truly new fingerprint
        v = Version(fingerprint=fp, content=content)
        self._fingerprints[fp] = v
        self._history.append(v)
        self._current_fp = fp
        return True, v, prev_version

    def history_summary(self) -> list[dict]:
        return [
            {
                "fp": v.fingerprint,
                "seen": v.seen_count,
                "duration_s": round(v.last_seen - v.first_seen, 1),
                "preview": v.content[:60]
            }
            for v in list(self._history)[-5:]
        ]

CONTENT_STREAM = [
    "config: model=haiku, max_tokens=256",
    "config: model=haiku, max_tokens=256",  # duplicate
    "config: model=haiku, max_tokens=512",  # changed
    "config: model=sonnet, max_tokens=512", # changed
    "config: model=sonnet, max_tokens=512", # duplicate
    "config: model=haiku, max_tokens=256",  # reverted
]

async def process_version_change(current: Version, previous: Version | None) -> str:
    if previous:
        context = f"Changed from '{previous.content[:80]}' to '{current.content[:80]}'"
    else:
        context = f"Initial state: '{current.content[:80]}'"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Describe this config change in one sentence: {context}"}]
    )
    return response.content[0].text if response.content else ""

async def fingerprint_monitor():
    tracker = FingerprintVersionTracker()
    for i, content in enumerate(CONTENT_STREAM):
        is_new, current, previous = tracker.record(content)
        if is_new:
            analysis = await process_version_change(current, previous)
            print(f"[{i+1}] NEW (fp={current.fingerprint}): {analysis[:100]}")
        else:
            print(f"[{i+1}] Duplicate (seen {current.seen_count}x) — skipped")

    print(f"\nVersion history:")
    for h in tracker.history_summary():
        print(f"  {h['fp']}: seen={h['seen']}, duration={h['duration_s']}s, '{h['preview']}'")

asyncio.run(fingerprint_monitor())
```

## Comparison

| Approach | Speed | Granularity | Token Savings | Best For |
|---|---|---|---|---|
| Hash-Based Detection | Fastest | Any/no change | Very High | Polling any content type |
| Structural JSON Diff | Fast | Field-level | High | API/structured data monitoring |
| Line-Level Text Diff | Fast | Line-level | High | Log/file monitoring |
| Semantic Detection | Slow (+LLM call) | Semantic meaning | High | Noisy data with false positives |
| Windowed Aggregation | Medium | Batched changes | High | High-frequency metric streams |
| Fingerprint Versioning | Fast | State versions | Very High | Config/state with reversions |

**Rule of thumb**: Always apply hash-based detection first (free, zero latency). Add structural diff when you need to know *what* changed. Use semantic detection only for high-value monitoring where noise causes alert fatigue.
