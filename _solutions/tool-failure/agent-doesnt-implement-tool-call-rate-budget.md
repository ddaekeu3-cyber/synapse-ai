---
layout: solution
title: "Agent Doesn't Implement Tool Call Rate Budget"
category: tool-failure
description: "Enforce per-session and per-tool call budgets so runaway agents can't exhaust external API quotas, rack up unexpected costs, or loop indefinitely — with hard limits, soft warnings, and per-tool spending caps tracked in SQLite."
tags: [tool-failure, rate-limiting, budget, cost-control, sqlite, python]
---

# Agent Doesn't Implement Tool Call Rate Budget

Agents without tool call budgets can loop indefinitely, exhaust third-party API quotas in a single session, or trigger unexpected billing spikes. A rate budget enforces hard limits per tool and per session — blocking calls when the budget is exhausted, surfacing warnings before the limit is reached, and logging spend for post-session analysis.

## Option 1: Simple Per-Session Call Counter

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ToolBudget:
    max_calls: int
    _used: int = field(default=0, init=False)

    def consume(self, tool_name: str) -> bool:
        if self._used >= self.max_calls:
            print(f"  [BUDGET] {tool_name}: budget exhausted ({self._used}/{self.max_calls})")
            return False
        self._used += 1
        remaining = self.max_calls - self._used
        if remaining <= 2:
            print(f"  [WARN  ] {tool_name}: {remaining} calls remaining")
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self._used)

# Tool implementations
def search_web(query: str) -> str:
    return f"Search result for: {query}"

def read_file(path: str) -> str:
    return f"Contents of {path}"

def write_file(path: str, content: str) -> str:
    return f"Written {len(content)} bytes to {path}"

TOOLS = [
    {"name": "search_web", "description": "Search the web",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file",  "description": "Read a file",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
]

def run_with_budget(task: str, budget: ToolBudget):
    messages = [{"role": "user", "content": task}]
    for _ in range(10):  # max turns
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            text = next((b.text for b in resp.content if hasattr(b, "text")), "")
            return text

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                if not budget.consume(block.name):
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": "ERROR: Tool call budget exhausted. Stop using tools.",
                        "is_error": True,
                    })
                    continue
                # Execute the tool
                fn_map = {"search_web": search_web, "read_file": read_file, "write_file": write_file}
                result = fn_map[block.name](**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})
    return "Task incomplete — max turns reached"

budget = ToolBudget(max_calls=3)
result = run_with_budget("Search for Python docs, read setup.py, then write a summary.", budget)
print(f"Result: {result[:100]}")
print(f"Budget used: {budget._used}/{budget.max_calls}")

# Expected Token Savings: Budget cap prevents infinite tool loops; exhaustion message ends tool use early
# Environment: adjust max_calls per task type; set lower budgets for untrusted user inputs
```

## Option 2: Per-Tool Budget with Different Limits

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class PerToolBudget:
    limits: dict[str, int]  # tool_name -> max calls
    _counts: dict[str, int] = field(default_factory=dict, init=False)

    def consume(self, tool_name: str) -> tuple[bool, str]:
        limit = self.limits.get(tool_name, 5)  # default 5 if not specified
        current = self._counts.get(tool_name, 0)
        if current >= limit:
            return False, f"{tool_name} budget exhausted ({current}/{limit})"
        self._counts[tool_name] = current + 1
        remaining = limit - self._counts[tool_name]
        if remaining == 1:
            return True, f"WARNING: last call allowed for {tool_name}"
        return True, "ok"

    def report(self) -> dict:
        return {
            tool: f"{self._counts.get(tool, 0)}/{self.limits.get(tool, 5)}"
            for tool in set(list(self.limits) + list(self._counts))
        }

def make_tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    }

TOOLS = [
    make_tool("expensive_api", "Call an expensive external API"),
    make_tool("cheap_lookup",  "Fast local lookup"),
    make_tool("write_output",  "Write to output"),
]

# Set tight budget for expensive operations, loose for cheap ones
budget = PerToolBudget(limits={
    "expensive_api": 2,   # very restricted
    "cheap_lookup":  10,  # generous
    "write_output":  3,   # moderate
})

def execute_tool(name: str, input_val: str) -> str:
    return f"{name}({input_val!r}) -> result"

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "done")

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                allowed, msg = budget.consume(block.name)
                if not allowed:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"BLOCKED: {msg}", "is_error": True})
                else:
                    if msg != "ok":
                        print(f"  {msg}")
                    result = execute_tool(block.name, block.input.get("input", ""))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [{"role": "assistant", "content": resp.content},
                     {"role": "user", "content": results}]
    return "incomplete"

result = run_agent("Use the APIs to gather information and write a summary.")
print(f"Done: {result[:80]}")
print(f"Budget report: {budget.report()}")

# Expected Token Savings: Per-tool limits prevent expensive_api monopolizing the session budget
# Environment: set limits from config; expensive_api budget reflects external API pricing tiers
```

## Option 3: Rate Budget with SQLite Spend Tracking

```python
import anthropic
import sqlite3
import time
import uuid

client = anthropic.Anthropic()
DB = "tool_budget.db"

# Estimated cost per tool call (in API credits or $)
TOOL_COSTS = {
    "web_search":   0.01,
    "db_query":     0.001,
    "llm_rerank":   0.05,
    "write_file":   0.0,
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tool_spend (
            session_id TEXT, tool TEXT, call_num INTEGER,
            cost REAL, ts REAL
        )
    """)
    con.commit(); con.close()

class CostBudget:
    def __init__(self, session_id: str, max_cost: float):
        self._session_id = session_id
        self._max_cost   = max_cost
        self._spent      = 0.0
        init_db()

    def consume(self, tool: str) -> tuple[bool, float]:
        cost = TOOL_COSTS.get(tool, 0.005)
        if self._spent + cost > self._max_cost:
            return False, cost
        self._spent += cost
        call_num = self._get_call_count(tool) + 1
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO tool_spend VALUES (?,?,?,?,?)",
                    (self._session_id, tool, call_num, cost, time.time()))
        con.commit(); con.close()
        remaining = self._max_cost - self._spent
        if remaining < self._max_cost * 0.1:
            print(f"  [WARN] Only ${remaining:.4f} budget remaining")
        return True, cost

    def _get_call_count(self, tool: str) -> int:
        con = sqlite3.connect(DB)
        n = con.execute("SELECT COUNT(*) FROM tool_spend WHERE session_id=? AND tool=?",
                        (self._session_id, tool)).fetchone()[0]
        con.close()
        return n

    def spend_report(self) -> list[dict]:
        con = sqlite3.connect(DB)
        rows = con.execute("""
            SELECT tool, COUNT(*) calls, SUM(cost) total_cost
            FROM tool_spend WHERE session_id=? GROUP BY tool
        """, (self._session_id,)).fetchall()
        con.close()
        return [{"tool": r[0], "calls": r[1], "cost": round(r[2], 4)} for r in rows]

def make_tools():
    return [
        {"name": t, "description": f"Tool: {t}",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}
        for t in TOOL_COSTS
    ]

session_id = str(uuid.uuid4())[:8]
budget = CostBudget(session_id, max_cost=0.025)

messages = [{"role": "user", "content": "Search for info, query DB twice, then write summary."}]
for _ in range(8):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=make_tools(),
        messages=messages,
    )
    if resp.stop_reason == "end_turn": break
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            allowed, cost = budget.consume(block.name)
            if not allowed:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"BLOCKED: cost budget (${budget._max_cost:.3f}) exceeded",
                                "is_error": True})
            else:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Result of {block.name}"})
    messages += [{"role": "assistant", "content": resp.content},
                 {"role": "user", "content": results}]

print(f"Session {session_id} spend: ${budget._spent:.4f}/${budget._max_cost:.4f}")
for row in budget.spend_report():
    print(f"  {row['tool']:15s} {row['calls']}x = ${row['cost']:.4f}")

# Expected Token Savings: Cost budget prevents a single session from using 100x average spend; SQLite tracks billing
# Environment: TOOL_COSTS tunable per pricing tier; max_cost set per user plan (free vs pro)
```

## Option 4: Sliding Window Rate Limiter for Tool Calls

```python
import anthropic
import time
from collections import deque

client = anthropic.Anthropic()

class SlidingWindowBudget:
    """Allow at most N tool calls per sliding window of W seconds."""
    def __init__(self, max_calls: int, window_s: float):
        self._max = max_calls
        self._window = window_s
        self._timestamps: dict[str, deque] = {}  # per tool

    def consume(self, tool: str) -> tuple[bool, str]:
        now = time.time()
        dq = self._timestamps.setdefault(tool, deque())
        # Evict timestamps outside the window
        while dq and now - dq[0] > self._window:
            dq.popleft()
        if len(dq) >= self._max:
            reset_in = self._window - (now - dq[0])
            return False, f"{tool}: rate limit ({self._max}/{self._window}s). Reset in {reset_in:.1f}s"
        dq.append(now)
        remaining = self._max - len(dq)
        return True, f"{tool}: {remaining} calls left in window"

    def calls_in_window(self, tool: str) -> int:
        now = time.time()
        dq = self._timestamps.get(tool, deque())
        return sum(1 for ts in dq if now - ts <= self._window)

def fake_tool(name: str, **kwargs) -> str:
    return f"{name} executed"

TOOLS = [
    {"name": "search", "description": "Search",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "lookup", "description": "Lookup",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]

# 3 searches per 30s, 10 lookups per 30s
budgets = {
    "search": SlidingWindowBudget(max_calls=3, window_s=30),
    "lookup": SlidingWindowBudget(max_calls=10, window_s=30),
}

messages = [{"role": "user", "content": "Search 5 times and lookup 3 times for a research task."}]
for _ in range(10):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOLS,
        messages=messages,
    )
    if resp.stop_reason == "end_turn": break
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            budget = budgets.get(block.name)
            if budget:
                allowed, msg = budget.consume(block.name)
                if not allowed:
                    print(f"  [RATE_LIMIT] {msg}")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Rate limited: {msg}", "is_error": True})
                    continue
            result = fake_tool(block.name, **block.input)
            print(f"  [TOOL] {block.name}: {msg if budget else 'no limit'}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
    messages += [{"role": "assistant", "content": resp.content},
                 {"role": "user", "content": results}]

for tool, budget in budgets.items():
    print(f"{tool}: {budget.calls_in_window(tool)}/{budget._max} in window")

# Expected Token Savings: Sliding window prevents burst abuse while allowing sustained reasonable usage
# Environment: window_s=60 for per-minute rate limits matching external APIs; per-tool limits match API pricing tiers
```

## Option 5: Hierarchical Budget — Session > Tool > Call

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class HierarchicalBudget:
    """Three-level budget: session total > per-tool total > per-minute rate."""
    session_max: int = 20          # total calls this session
    per_tool_max: dict = field(default_factory=dict)  # per-tool limits
    _session_used: int = field(default=0, init=False)
    _tool_used: dict = field(default_factory=dict, init=False)

    def consume(self, tool: str) -> tuple[bool, str]:
        # Level 1: session budget
        if self._session_used >= self.session_max:
            return False, f"Session budget exhausted ({self._session_used}/{self.session_max})"

        # Level 2: per-tool budget
        tool_limit = self.per_tool_max.get(tool, 10)
        tool_used  = self._tool_used.get(tool, 0)
        if tool_used >= tool_limit:
            return False, f"{tool} budget exhausted ({tool_used}/{tool_limit})"

        self._session_used += 1
        self._tool_used[tool] = tool_used + 1

        sess_remaining = self.session_max - self._session_used
        tool_remaining = tool_limit - self._tool_used[tool]
        if sess_remaining <= 3:
            return True, f"SESSION WARNING: {sess_remaining} calls left"
        if tool_remaining <= 1:
            return True, f"TOOL WARNING: last call for {tool}"
        return True, "ok"

    def status(self) -> dict:
        return {
            "session": f"{self._session_used}/{self.session_max}",
            "per_tool": {t: f"{self._tool_used.get(t,0)}/{self.per_tool_max.get(t,10)}"
                         for t in set(list(self.per_tool_max) + list(self._tool_used))},
        }

TOOLS = [
    {"name": t, "description": f"Tool {t}",
     "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}
    for t in ["api_call", "db_query", "file_write"]
]

budget = HierarchicalBudget(
    session_max=8,
    per_tool_max={"api_call": 2, "db_query": 5, "file_write": 3},
)

messages = [{"role": "user", "content": "Use all available tools repeatedly to complete a research task."}]
for _ in range(15):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOLS,
        messages=messages,
    )
    if resp.stop_reason == "end_turn": break
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            allowed, msg = budget.consume(block.name)
            if not allowed:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"BLOCKED: {msg}", "is_error": True})
            else:
                if msg != "ok": print(f"  [{msg}]")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"{block.name} result"})
    messages += [{"role": "assistant", "content": resp.content},
                 {"role": "user", "content": results}]

status = budget.status()
print(f"Session used: {status['session']}")
for tool, usage in status["per_tool"].items():
    print(f"  {tool:12s}: {usage}")

# Expected Token Savings: Hierarchical limits catch both global runaway and single-tool abuse
# Environment: tune per_tool_max to reflect external API pricing; session_max from user tier settings
```

## Option 6: Budget with Webhook Alert on Near-Exhaustion

```python
import anthropic
import sqlite3
import time
import json
import urllib.request
import urllib.error

client = anthropic.Anthropic()
DB = "budget_alerts.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS budget_events (
            ts REAL, session_id TEXT, tool TEXT,
            event TEXT, used INTEGER, limit_ INTEGER
        )
    """)
    con.commit(); con.close()

class AlertingBudget:
    def __init__(self, session_id: str, limits: dict[str, int],
                 warn_pct: float = 0.8, webhook_url: str | None = None):
        self._session_id = session_id
        self._limits     = limits
        self._used: dict[str, int] = {}
        self._warn_pct   = warn_pct
        self._webhook    = webhook_url
        self._alerted: set = set()
        init_db()

    def _log(self, tool: str, event: str):
        used  = self._used.get(tool, 0)
        limit = self._limits.get(tool, 999)
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO budget_events VALUES (?,?,?,?,?,?)",
                    (time.time(), self._session_id, tool, event, used, limit))
        con.commit(); con.close()

    def _alert(self, tool: str, message: str):
        alert_key = f"{tool}:{message[:20]}"
        if alert_key in self._alerted:
            return
        self._alerted.add(alert_key)
        print(f"  [ALERT] {message}")
        self._log(tool, f"alert:{message[:40]}")
        if self._webhook:
            payload = json.dumps({"session": self._session_id, "tool": tool, "alert": message})
            try:
                req = urllib.request.Request(
                    self._webhook, data=payload.encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(req, timeout=3)
            except urllib.error.URLError:
                pass

    def consume(self, tool: str) -> bool:
        limit = self._limits.get(tool, 10)
        used  = self._used.get(tool, 0)
        if used >= limit:
            self._alert(tool, f"{tool} exhausted ({used}/{limit})")
            return False
        self._used[tool] = used + 1
        warn_threshold = int(limit * self._warn_pct)
        if self._used[tool] >= warn_threshold:
            self._alert(tool, f"{tool} at {self._used[tool]}/{limit} ({self._warn_pct:.0%})")
        return True

    def summary(self) -> list[dict]:
        con = sqlite3.connect(DB)
        rows = con.execute(
            "SELECT tool, event, COUNT(*) FROM budget_events WHERE session_id=? GROUP BY tool, event",
            (self._session_id,),
        ).fetchall()
        con.close()
        return [{"tool": r[0], "event": r[1], "count": r[2]} for r in rows]

TOOLS = [
    {"name": t, "description": f"Tool {t}",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}
    for t in ["search", "analyze", "export"]
]
budget = AlertingBudget("sess-001", {"search": 3, "analyze": 5, "export": 2}, warn_pct=0.7)
messages = [{"role": "user", "content": "Search and analyze data, then export results."}]
for _ in range(10):
    resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256,
                                   tools=TOOLS, messages=messages)
    if resp.stop_reason == "end_turn": break
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            if budget.consume(block.name):
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": "ok"})
            else:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "BLOCKED", "is_error": True})
    messages += [{"role": "assistant", "content": resp.content},
                 {"role": "user", "content": results}]
print("\nBudget summary:")
for row in budget.summary():
    print(f"  {row['tool']:10s} [{row['event']:30s}] x{row['count']}")

# Expected Token Savings: Webhook alert fires before exhaustion; ops team can intervene before session is blocked
# Environment: set WEBHOOK_URL to Slack/PagerDuty; warn_pct=0.8 gives early warning at 80% usage
```

## Comparison

| Option | Budget Type | Per-Tool Limits | Spend Tracking | Alerts |
|--------|------------|----------------|---------------|--------|
| 1 — Simple Counter | Total calls | No | No | Remaining count |
| 2 — Per-Tool Limits | Per-tool calls | Yes | No | Remaining count |
| 3 — Cost Tracking | Dollar budget | No | SQLite | Low balance warn |
| 4 — Sliding Window | Rate per window | Per-tool rates | Timestamps | Rate limit msg |
| 5 — Hierarchical | Session + per-tool | Yes | No | Threshold warnings |
| 6 — Alerting Budget | Per-tool calls | Yes | SQLite | Webhook |
