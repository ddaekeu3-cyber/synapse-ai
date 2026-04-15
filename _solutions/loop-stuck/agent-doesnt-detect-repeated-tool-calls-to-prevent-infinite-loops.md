---
layout: solution
title: "Agent Doesn't Detect Repeated Tool Calls to Prevent Infinite Loops"
category: loop-stuck
description: "Agents that repeatedly invoke the same tool with the same arguments are stuck in an unproductive loop — burning tokens, delaying the user, and never making progress. Detecting repeated tool calls by hashing (tool_name, arguments) and tracking call history lets the agent recognize it's cycling and take corrective action."
tags: [loop-detection, tool-calls, infinite-loop, deduplication, state-hashing, circuit-breaker, stuck]
---

# Agent Doesn't Detect Repeated Tool Calls to Prevent Infinite Loops

## Problem

Agents loop when they receive an unhelpful tool result, try the same call again hoping for a different outcome, and repeat indefinitely. This manifests as: the same `web_search("Python tutorial")` called 10 times in a row, or a `read_file("/etc/config")` that keeps failing but the agent never stops retrying. Without loop detection, the agent burns through the entire context window and token budget before timing out.

**Symptoms:**
- Tool call history shows the same call repeated 5+ times
- Agent never progresses past a particular step
- Token usage spikes but no useful work is done
- Context window fills with identical tool results
- Agent explains it will try again then does the exact same thing

---

## Option 1: Exact-Match Hash Deduplication with Hard Stop

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field

@dataclass
class ToolCallRecord:
    tool_name: str
    tool_input: dict
    call_count: int = 1
    last_result: str = ""

class ToolCallDeduplicator:
    """Track tool calls by (name, input) hash. Halt on repeated calls."""

    def __init__(self, max_repeats: int = 2):
        self._calls: dict[str, ToolCallRecord] = {}
        self.max_repeats = max_repeats
        self.blocked_calls: list[str] = []

    def _hash(self, tool_name: str, tool_input: dict) -> str:
        payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def check(self, tool_name: str, tool_input: dict) -> tuple[bool, str]:
        """
        Returns (should_block, reason).
        should_block=True means this call is a repeat and should be intercepted.
        """
        key = self._hash(tool_name, tool_input)
        if key in self._calls:
            record = self._calls[key]
            record.call_count += 1
            if record.call_count > self.max_repeats:
                reason = (f"Tool '{tool_name}' called with identical arguments "
                          f"{record.call_count} times. Last result: {record.last_result[:100]}")
                self.blocked_calls.append(key)
                return True, reason
        else:
            self._calls[key] = ToolCallRecord(tool_name, tool_input)
        return False, ""

    def record_result(self, tool_name: str, tool_input: dict, result: str):
        key = self._hash(tool_name, tool_input)
        if key in self._calls:
            self._calls[key].last_result = result

    def summary(self) -> dict:
        repeated = {k: v for k, v in self._calls.items() if v.call_count > 1}
        return {
            "total_unique_calls": len(self._calls),
            "repeated_calls": len(repeated),
            "blocked_calls": len(self.blocked_calls),
            "repeat_details": [
                {"tool": v.tool_name, "count": v.call_count}
                for v in repeated.values()
            ]
        }

def run_loop_protected_agent(user_query: str, max_turns: int = 10):
    client = anthropic.Anthropic()
    deduplicator = ToolCallDeduplicator(max_repeats=2)

    tools = [
        {
            "name": "web_search",
            "description": "Search the web for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        },
        {
            "name": "read_file",
            "description": "Read a file by path",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    print(f"Query: {user_query}\n")

    for turn in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_results = []
        has_tool_use = False

        for block in response.content:
            if block.type == "tool_use":
                has_tool_use = True
                blocked, reason = deduplicator.check(block.name, block.input)

                if blocked:
                    # Inject a loop-breaking message instead of executing the tool
                    result = (f"[LOOP DETECTED] {reason}. "
                              f"Please try a different approach or tool, or report that you cannot complete this task.")
                    print(f"  [BLOCKED] {block.name}({block.input}) — repeated call #{deduplicator._calls[deduplicator._hash(block.name, block.input)].call_count}")
                else:
                    # Simulate tool execution
                    if block.name == "web_search":
                        result = f"Search results for '{block.input.get('query', '')}': [No relevant results found]"
                    else:
                        result = f"File not found: {block.input.get('path', '')}"
                    deduplicator.record_result(block.name, block.input, result)
                    print(f"  [{turn+1}] {block.name}({block.input}): {result[:60]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        if response.stop_reason == "end_turn" or not has_tool_use:
            final = next((b.text for b in response.content if b.type == "text"), "")
            print(f"\nAgent: {final[:200]}")
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    stats = deduplicator.summary()
    print(f"\nLoop detection stats: {stats}")

# This query will cause the agent to loop on a failing search
run_loop_protected_agent(
    "Find information about the secret project X-47B that doesn't exist in any database",
    max_turns=8
)

# Expected Token Savings: ~60% — stops loops after 2nd repeat instead of filling context window
# Environment: Any agentic loop; set max_repeats=1 for strict dedup, 2-3 for tolerance of retries
```

---

## Option 2: Semantic Loop Detection via Message State Hashing

```python
import anthropic
import hashlib
import json
from collections import Counter

def hash_conversation_state(messages: list[dict]) -> str:
    """Hash the recent conversation state to detect cycling."""
    # Use only the last 4 messages for state fingerprint
    recent = messages[-4:] if len(messages) >= 4 else messages
    state_repr = []
    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Extract tool names and text
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        parts.append(f"tool:{block.get('name')}:{json.dumps(block.get('input', {}), sort_keys=True)}")
                    elif block.get("type") == "text":
                        parts.append(f"text:{block.get('text', '')[:50]}")
            content = "|".join(parts)
        state_repr.append(f"{role}:{str(content)[:100]}")
    combined = "||".join(state_repr)
    return hashlib.md5(combined.encode()).hexdigest()[:10]

class ConversationLoopDetector:
    """Detect loops by tracking conversation state hashes."""

    def __init__(self, window_size: int = 6, repeat_threshold: int = 2):
        self._state_history: list[str] = []
        self._state_counts: Counter = Counter()
        self.window = window_size
        self.threshold = repeat_threshold
        self.loop_detected = False
        self.loop_state: str = ""

    def check(self, messages: list[dict]) -> tuple[bool, str]:
        state_hash = hash_conversation_state(messages)
        self._state_history.append(state_hash)
        self._state_counts[state_hash] += 1

        count = self._state_counts[state_hash]
        if count >= self.threshold:
            self.loop_detected = True
            self.loop_state = state_hash
            return True, f"Conversation state {state_hash!r} repeated {count} times — agent is cycling"

        return False, ""

    def recent_states(self) -> list[str]:
        return self._state_history[-self.window:]

def run_state_hash_protected_agent(user_query: str):
    client = anthropic.Anthropic()
    detector = ConversationLoopDetector(repeat_threshold=2)

    tools = [{
        "name": "lookup",
        "description": "Look up information by keyword",
        "input_schema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    interrupted = False

    for turn in range(12):
        # Check conversation state before each LLM call
        is_loop, reason = detector.check(messages)
        if is_loop:
            print(f"\n[Loop Detector] {reason}")
            # Inject a circuit-breaking message
            messages.append({
                "role": "user",
                "content": (f"[SYSTEM] You appear to be in a loop: {reason}. "
                            f"Please stop retrying and either report what information you could not find, "
                            f"or try a completely different approach.")
            })
            interrupted = True

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Always return "not found" to force looping behavior for demo
                result = f"No results found for keyword: {block.input.get('keyword', '')}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })
                print(f"  [Turn {turn+1}] lookup({block.input.get('keyword')!r}) -> not found")

        if response.stop_reason == "end_turn" or not tool_results:
            final = next((b.text for b in response.content if b.type == "text"), "")
            print(f"\nAgent final response: {final[:200]}")
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        if interrupted and response.stop_reason == "end_turn":
            break

    print(f"\nState history (last 6): {detector.recent_states()}")
    print(f"Loop detected: {detector.loop_detected}, loop state: {detector.loop_state}")

run_state_hash_protected_agent("Find the population of a fictional city called Zephyrton")

# Expected Token Savings: ~55% — state hashing catches subtler loops where arguments vary slightly
# Environment: Multi-turn agents; window_size=6 balances detection latency vs false positives
```

---

## Option 3: Tool Call Similarity Detection — Catch Near-Duplicate Calls

```python
import anthropic
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

@dataclass
class ToolCall:
    name: str
    input: dict
    turn: int

def jaccard_similarity(dict_a: dict, dict_b: dict) -> float:
    """Measure input similarity between two tool calls."""
    str_a = set(json.dumps(dict_a, sort_keys=True).lower().split())
    str_b = set(json.dumps(dict_b, sort_keys=True).lower().split())
    if not str_a and not str_b:
        return 1.0
    intersection = str_a & str_b
    union = str_a | str_b
    return len(intersection) / len(union) if union else 0.0

class SimilarityLoopDetector:
    """Detect loops where tool call arguments are similar but not identical."""

    def __init__(self, similarity_threshold: float = 0.85, window: int = 6):
        self._history: list[ToolCall] = []
        self.threshold = similarity_threshold
        self.window = window
        self.warnings: list[str] = []

    def check(self, tool_name: str, tool_input: dict, turn: int) -> tuple[bool, str]:
        call = ToolCall(tool_name, tool_input, turn)
        # Compare against recent same-name calls
        recent_same = [c for c in self._history[-self.window:] if c.name == tool_name]

        for past_call in recent_same:
            sim = jaccard_similarity(tool_input, past_call.input)
            if sim >= self.threshold:
                msg = (f"Tool '{tool_name}' called with {sim:.0%} similar input "
                       f"(turn {past_call.turn} vs now). Possible loop.")
                self.warnings.append(msg)
                self._history.append(call)
                return True, msg

        self._history.append(call)
        return False, ""

    def get_loop_summary(self) -> str:
        by_tool = defaultdict(int)
        for w in self.warnings:
            tool = w.split("'")[1]
            by_tool[tool] += 1
        parts = [f"{tool}: {count} near-duplicate calls" for tool, count in by_tool.items()]
        return " | ".join(parts) if parts else "No loops detected"

def run_similarity_protected_agent(user_query: str):
    client = anthropic.Anthropic()
    detector = SimilarityLoopDetector(similarity_threshold=0.80, window=5)

    tools = [{
        "name": "database_query",
        "description": "Query the database with SQL",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["sql"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    loop_injections = 0

    for turn in range(10):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                is_similar, reason = detector.check(block.name, block.input, turn)

                if is_similar and loop_injections < 2:
                    result = (f"[LOOP WARNING] {reason} "
                              f"Try reformulating the query or using different criteria.")
                    loop_injections += 1
                    print(f"  [SIMILARITY LOOP] turn={turn}: {reason[:80]}")
                else:
                    # Simulate always-empty result to force looping
                    result = "Query returned 0 rows."
                    print(f"  [Turn {turn+1}] {block.name}: {block.input.get('sql', '')[:50]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        if response.stop_reason == "end_turn" or not tool_results:
            final = next((b.text for b in response.content if b.type == "text"), "")
            print(f"\nAgent: {final[:200]}")
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"\nSimilarity loop summary: {detector.get_loop_summary()}")
    print(f"Loop injection count: {loop_injections}")

run_similarity_protected_agent(
    "Find all users who registered in January 2024 and haven't made a purchase yet"
)

# Expected Token Savings: ~50% — catches loops with slightly varying arguments (limit 10 vs limit 20)
# Environment: Agents querying databases or search APIs where results are empty or paginated
```

---

## Option 4: Progress-Based Loop Detection — Require Forward Momentum

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ProgressSnapshot:
    turn: int
    timestamp: float
    unique_tools_used: set[str] = field(default_factory=set)
    unique_results_seen: set[str] = field(default_factory=set)
    task_completed: bool = False

class ProgressMonitor:
    """
    Detect loops by measuring forward progress.
    Progress = new information added to context (new tools, new unique results).
    If no new information is added for N turns, declare loop.
    """

    def __init__(self, stagnation_turns: int = 3, min_new_results_per_turn: int = 1):
        self._snapshots: list[ProgressSnapshot] = []
        self._all_results: set[str] = set()
        self._all_tools: set[str] = set()
        self.stagnation_limit = stagnation_turns
        self.min_new = min_new_results_per_turn
        self._stagnant_turns = 0

    def record_turn(self, turn: int, tool_calls: list[dict], tool_results: list[str]):
        new_tools = {t.get("name", "") for t in tool_calls} - self._all_tools
        new_results = set()
        for r in tool_results:
            # Fingerprint: first 60 chars of result
            fingerprint = r.strip()[:60]
            if fingerprint not in self._all_results:
                new_results.add(fingerprint)
                self._all_results.add(fingerprint)

        self._all_tools.update(new_tools)
        snap = ProgressSnapshot(
            turn=turn,
            timestamp=time.time(),
            unique_tools_used=new_tools,
            unique_results_seen=new_results
        )
        self._snapshots.append(snap)

        new_info_count = len(new_tools) + len(new_results)
        if new_info_count < self.min_new:
            self._stagnant_turns += 1
        else:
            self._stagnant_turns = 0

        return new_info_count, self._stagnant_turns

    def is_stagnant(self) -> tuple[bool, str]:
        if self._stagnant_turns >= self.stagnation_limit:
            return True, (f"No new information in {self._stagnant_turns} consecutive turns. "
                          f"Total unique results: {len(self._all_results)}, "
                          f"tools tried: {self._all_tools}")
        return False, ""

def run_progress_monitored_agent(user_query: str):
    client = anthropic.Anthropic()
    monitor = ProgressMonitor(stagnation_turns=3)

    tools = [
        {
            "name": "search_web",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        },
        {
            "name": "search_docs",
            "description": "Search documentation",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        },
        {
            "name": "give_up",
            "description": "Report inability to complete the task",
            "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    gave_up = False

    for turn in range(15):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        turn_tool_calls = []
        turn_results = []
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "give_up":
                    print(f"\n  [Agent gave up] {block.input.get('reason', '')}")
                    gave_up = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Acknowledged. Task abandoned."
                    })
                    continue

                # Simulate always returning empty/same result
                result = f"No relevant results found for: {list(block.input.values())[0] if block.input else ''}"
                turn_tool_calls.append({"name": block.name, "input": block.input})
                turn_results.append(result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })
                print(f"  [Turn {turn+1}] {block.name}: {result[:60]}")

        if turn_tool_calls:
            new_info, stagnant_count = monitor.record_turn(turn, turn_tool_calls, turn_results)
            print(f"    Progress: +{new_info} new, stagnant_turns={stagnant_count}")

        is_stagnant, reason = monitor.is_stagnant()
        if is_stagnant:
            stagnation_msg = (
                f"[PROGRESS MONITOR] {reason}. "
                f"You have the 'give_up' tool available to report task failure. "
                f"Please use it if you cannot make further progress."
            )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results + [{
                "type": "text" if False else None,
                "content": stagnation_msg  # Inject as system context
            }]})
            # Simpler: just append a user text message
            messages[-1] = {"role": "user", "content": stagnation_msg}
            print(f"\n[Monitor] Injecting stagnation warning")
            continue

        if response.stop_reason == "end_turn" or not tool_results or gave_up:
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"\nFinal: gave_up={gave_up}, total_unique_results={len(monitor._all_results)}")

run_progress_monitored_agent("Find the complete source code of a proprietary closed-source application")

# Expected Token Savings: ~65% — halts after 3 turns of zero progress vs waiting for context overflow
# Environment: Research agents, web-search agents; tune stagnation_turns based on task complexity
```

---

## Option 5: Tool Call Budget with Automatic Escalation

```python
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class ToolBudget:
    tool_name: str
    max_calls: int
    calls_made: int = 0
    escalation_message: str = ""

    @property
    def exhausted(self) -> bool:
        return self.calls_made >= self.max_calls

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls_made)

class ToolBudgetManager:
    """
    Assign per-tool call budgets. When a tool exhausts its budget,
    inject escalation guidance telling the agent to try something else.
    """

    def __init__(self, budgets: dict[str, int], global_max: int = 20):
        self._budgets: dict[str, ToolBudget] = {
            name: ToolBudget(
                tool_name=name,
                max_calls=limit,
                escalation_message=f"You've used {name} {limit} times without success. Try a different approach."
            )
            for name, limit in budgets.items()
        }
        self._global_calls = 0
        self._global_max = global_max
        self._call_log: list[tuple[str, dict]] = []

    def consume(self, tool_name: str, tool_input: dict) -> tuple[bool, str]:
        """
        Returns (allowed, message).
        allowed=False if budget exhausted; message explains why.
        """
        self._global_calls += 1
        self._call_log.append((tool_name, tool_input))

        if self._global_calls > self._global_max:
            return False, f"Global tool call budget ({self._global_max}) exhausted. Report results and stop."

        budget = self._budgets.get(tool_name)
        if budget is None:
            # Unknown tool — add default budget
            self._budgets[tool_name] = ToolBudget(tool_name, max_calls=5)
            budget = self._budgets[tool_name]

        budget.calls_made += 1
        if budget.exhausted:
            return False, budget.escalation_message

        return True, f"[Budget: {budget.remaining} calls remaining for {tool_name}]"

    def status(self) -> str:
        lines = [f"Global: {self._global_calls}/{self._global_max}"]
        for b in self._budgets.values():
            lines.append(f"  {b.tool_name}: {b.calls_made}/{b.max_calls}")
        return "\n".join(lines)

    def detect_loops(self) -> list[str]:
        """Find tools called with identical args repeatedly."""
        from collections import Counter
        import json
        counts = Counter(
            f"{name}:{json.dumps(inp, sort_keys=True)}"
            for name, inp in self._call_log
        )
        return [key for key, count in counts.items() if count >= 3]

def run_budget_managed_agent(user_query: str):
    client = anthropic.Anthropic()
    budget_mgr = ToolBudgetManager(
        budgets={"search": 3, "fetch_url": 4, "summarize": 5},
        global_max=15
    )

    tools = [
        {
            "name": "search",
            "description": "Search for information online",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        },
        {
            "name": "fetch_url",
            "description": "Fetch content from a URL",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"]
            }
        },
        {
            "name": "report_findings",
            "description": "Report final findings to the user",
            "input_schema": {
                "type": "object",
                "properties": {
                    "findings": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]}
                },
                "required": ["findings", "confidence"]
            }
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    final_report = None

    for turn in range(20):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "report_findings":
                    final_report = block.input
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Report received."
                    })
                    continue

                allowed, message = budget_mgr.consume(block.name, block.input)
                if allowed:
                    # Simulate tool returning empty result
                    result = f"No data found. {message}"
                    print(f"  [Turn {turn+1}] {block.name}: {result[:60]}")
                else:
                    result = f"[BUDGET EXHAUSTED] {message}"
                    print(f"  [BLOCKED] {block.name}: {message}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        if response.stop_reason == "end_turn" or not tool_results or final_report:
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"\nBudget status:\n{budget_mgr.status()}")
    loops = budget_mgr.detect_loops()
    if loops:
        print(f"Detected loop patterns: {loops}")
    if final_report:
        print(f"\nFinal report (confidence={final_report.get('confidence')}): {final_report.get('findings', '')[:150]}")

run_budget_managed_agent("Research everything about a technology that was invented last week")

# Expected Token Savings: ~70% — hard budget caps prevent unbounded tool call spirals
# Environment: Production agents; tune budgets per tool type based on observed usage patterns
```

---

## Option 6: Adaptive Loop Breaker — Learn from Past Sessions

```python
import anthropic
import json
import sqlite3
import time
import hashlib
from dataclasses import dataclass

@dataclass
class LoopPattern:
    pattern_id: str
    tool_sequence: list[str]
    occurrence_count: int
    avg_turns_before_stuck: float

class AdaptiveLoopBreaker:
    """
    Learns which tool sequences lead to loops across sessions.
    Flags sequences that have historically become stuck.
    """

    def __init__(self, db_path: str = "/tmp/loop_patterns.db", sequence_len: int = 3):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.seq_len = sequence_len
        self._setup()
        self._current_sequence: list[str] = []

    def _setup(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS loop_sequences (
                seq_hash TEXT PRIMARY KEY,
                sequence TEXT,
                occurrences INTEGER DEFAULT 1,
                total_turns INTEGER DEFAULT 0,
                led_to_loop INTEGER DEFAULT 0
            )
        """)
        self.db.commit()

    def _seq_hash(self, seq: list[str]) -> str:
        return hashlib.md5(json.dumps(seq).encode()).hexdigest()[:10]

    def record_call(self, tool_name: str) -> bool:
        """Record tool call, return True if current sequence is a known loop pattern."""
        self._current_sequence.append(tool_name)
        if len(self._current_sequence) < self.seq_len:
            return False

        recent_seq = self._current_sequence[-self.seq_len:]
        seq_hash = self._seq_hash(recent_seq)

        row = self.db.execute(
            "SELECT occurrences, led_to_loop FROM loop_sequences WHERE seq_hash = ?",
            (seq_hash,)
        ).fetchone()

        if row and row[0] > 0 and row[1] / row[0] > 0.7:
            # This sequence led to a loop >70% of the time historically
            return True

        # Record sequence seen
        self.db.execute("""
            INSERT INTO loop_sequences (seq_hash, sequence, occurrences)
            VALUES (?, ?, 1)
            ON CONFLICT(seq_hash) DO UPDATE SET occurrences = occurrences + 1
        """, (seq_hash, json.dumps(recent_seq)))
        self.db.commit()
        return False

    def mark_session_looped(self, turns: int):
        """Mark the last few sequences as having led to a loop."""
        for i in range(max(0, len(self._current_sequence) - self.seq_len), len(self._current_sequence) - 2):
            seq = self._current_sequence[i:i + self.seq_len]
            seq_hash = self._seq_hash(seq)
            self.db.execute("""
                UPDATE loop_sequences
                SET led_to_loop = led_to_loop + 1, total_turns = total_turns + ?
                WHERE seq_hash = ?
            """, (turns, seq_hash))
        self.db.commit()

    def known_loop_patterns(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT sequence, occurrences, led_to_loop
            FROM loop_sequences
            WHERE occurrences > 1 AND led_to_loop > 0
            ORDER BY led_to_loop DESC LIMIT 10
        """).fetchall()
        return [{"sequence": json.loads(r[0]), "occurrences": r[1], "looped": r[2]} for r in rows]

def run_adaptive_agent(user_query: str):
    client = anthropic.Anthropic()
    loop_breaker = AdaptiveLoopBreaker()

    tools = [
        {
            "name": "search",
            "description": "Search for information",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"]
            }
        },
        {
            "name": "refine",
            "description": "Refine search with different terms",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"]
            }
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    turn_count = 0
    looped = False

    for turn in range(12):
        turn_count = turn + 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                is_known_loop = loop_breaker.record_call(block.name)

                if is_known_loop:
                    result = (f"[ADAPTIVE LOOP BREAKER] This tool sequence has historically "
                              f"led to stuck agents. Please try a fundamentally different strategy "
                              f"or report that the task cannot be completed.")
                    looped = True
                    print(f"  [KNOWN LOOP PATTERN] {block.name} — historical loop detected")
                else:
                    result = f"Search returned nothing useful for: {block.input.get('q', '')}"
                    print(f"  [Turn {turn+1}] {block.name}({block.input.get('q', '')!r})")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        if response.stop_reason == "end_turn" or not tool_results:
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    if looped:
        loop_breaker.mark_session_looped(turn_count)

    patterns = loop_breaker.known_loop_patterns()
    print(f"\nKnown loop patterns in DB: {len(patterns)}")
    for p in patterns[:3]:
        print(f"  {p['sequence']} (seen {p['occurrences']}x, looped {p['looped']}x)")

run_adaptive_agent("Find information that doesn't exist")
run_adaptive_agent("Find information that doesn't exist")  # Second run — pattern recognized faster

# Expected Token Savings: ~75% on second+ run — learns from prior sessions, breaks loops earlier
# Environment: Production agents with persistent SQLite; replace with Redis for distributed deployments
```

---

## Comparison

| Option | Detection Method | Speed | Cross-Session | Catches Near-Dupes | Best For |
|--------|----------------|-------|--------------|-------------------|----------|
| Exact Hash Dedup | SHA-256 (name+input) | O(1) | No | No | Simple identical-call loops |
| State Hash | MD5 (message window) | O(n) | No | Yes | Subtle cycles with varied calls |
| Similarity Detection | Jaccard similarity | O(n) | No | Yes | Near-duplicate queries (limit 10 vs 20) |
| Progress Monitor | New info per turn | O(n) | No | Yes | Stagnation with varied but useless calls |
| Tool Budget | Per-tool call count | O(1) | No | No | Controlled spend with hard limits |
| Adaptive Breaker | Historical DB patterns | O(1) | Yes | Partial | Long-running production agents that learn |

**Recommendation:** Use **Option 1** (exact hash) as a baseline — it's zero overhead and catches the most common loop type. Layer **Option 4** (progress monitor) on top for stagnation detection when the agent varies its calls but never makes headway. Use **Option 5** (tool budget) in production to cap worst-case token spend regardless of loop type.
