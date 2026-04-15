---
layout: solution
title: "Agent Retries Same Failed Action Without Variation"
category: loop-stuck
description: "Agent calls the same tool with identical arguments after failure, looping on the same error instead of diagnosing and adapting."
tags: [loop-stuck, retry, error-recovery, adaptation, tool-use]
---

## Symptom

An agent encounters an error, then calls the exact same tool with the exact same arguments again. And again. Until it hits the context limit or max turns:

```
Turn 1: search_web(query="best Python web framework 2024") → HTTP 429
Turn 2: search_web(query="best Python web framework 2024") → HTTP 429
Turn 3: search_web(query="best Python web framework 2024") → HTTP 429
... (repeats 8 more times)
Turn 11: "I'm having trouble searching. Let me try..." search_web(query="best Python web framework 2024") → HTTP 429
```

Or more subtly, the agent varies its prose but not its actions:

```
Turn 4: "Let me try a different approach." → query_database(sql="SELECT * FROM users WHERE id=99")
Turn 5: "I'll attempt this again."         → query_database(sql="SELECT * FROM users WHERE id=99")
```

Root causes:
- No retry budget tracking — agent doesn't know it already tried
- Error message not analyzed for root cause before retry
- System prompt doesn't specify retry strategy
- Agent conflates "trying again" in narrative with actually changing the approach
- No exponential backoff — instant retries overwhelm rate-limited endpoints

---

## Root Cause

The model's training encourages persistence and trying again when something fails — this is generally helpful behavior. But it doesn't distinguish between errors where retry makes sense (transient network error) and errors where retry is futile (invalid input, resource not found, permission denied).

Additionally, the conversation history doesn't automatically cause the model to notice it already tried the same thing. Without explicit retry tracking and adaptation requirements, the model retries identically because the next token that follows a "try again" intention is the same tool call it just made.

---

## Fix

### Option 1 — Retry Budget with Forced Variation

Track retry attempts per tool+input hash and require the agent to change approach after N identical failures.

```python
import anthropic
import hashlib
import json
from collections import defaultdict
from typing import Any

client = anthropic.Anthropic()

class RetryBudgetTracker:
    """
    Tracks retry attempts per (tool_name, input_hash) pair.
    Injects an error if the same call is attempted too many times.
    """

    def __init__(self, max_identical_retries: int = 2):
        self.max_retries = max_identical_retries
        self.attempt_counts: dict[str, int] = defaultdict(int)
        self.error_history: dict[str, list[str]] = defaultdict(list)

    def _call_key(self, tool_name: str, tool_input: dict) -> str:
        """Stable hash of (tool_name, tool_input) for tracking."""
        canonical = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.md5(canonical.encode()).hexdigest()[:12]

    def record_attempt(self, tool_name: str, tool_input: dict, error: str = None) -> tuple[bool, str]:
        """
        Record an attempt. Returns (should_allow, message).
        If attempts exceeded, returns (False, guidance_message).
        """
        key = self._call_key(tool_name, tool_input)
        self.attempt_counts[key] += 1
        count = self.attempt_counts[key]

        if error:
            self.error_history[key].append(error)

        if count > self.max_retries:
            prior_errors = self.error_history.get(key, [])
            return False, (
                f"RETRY_LIMIT_REACHED: You have called {tool_name} with these exact arguments "
                f"{count} times and received the same errors. "
                f"Errors seen: {prior_errors}. "
                f"You MUST change your approach: try different arguments, a different tool, "
                f"or tell the user you cannot complete this task as requested. "
                f"Do NOT call {tool_name} with the same arguments again."
            )

        remaining = self.max_retries - count + 1
        return True, f"Attempt {count}/{self.max_retries + 1} for this call."

tracker = RetryBudgetTracker(max_identical_retries=2)

# Simulated tools
def search_web(query: str) -> dict:
    # Simulates persistent failure
    raise Exception("HTTP 429: Rate limited. Retry-After: 60")

def search_cache(query: str) -> dict:
    return {"results": [{"title": "FastAPI vs Flask", "snippet": "FastAPI is faster for async..."}]}

def answer_from_knowledge(topic: str) -> dict:
    return {"answer": f"Based on general knowledge about {topic}..."}

tools = [
    {
        "name": "search_web",
        "description": "Search the web for current information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "search_cache",
        "description": "Search a local cache of recent web results",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "answer_from_knowledge",
        "description": "Answer from training knowledge when search is unavailable",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    },
]

SYSTEM = """You are a research assistant.
When a tool fails, diagnose WHY before retrying.
- Transient errors (network timeout): retry ONCE with the same args
- Rate limit errors: switch to an alternative tool
- Not found errors: change your search query
- Repeated failures: tell the user and try a completely different approach
Never call the same tool with identical arguments more than twice."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    max_turns = 10

    for _ in range(max_turns):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            # Check retry budget before executing
            allowed, budget_msg = tracker.record_attempt(block.name, block.input)

            if not allowed:
                # Inject retry limit error — forces agent to change approach
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": budget_msg,
                    "is_error": True,
                })
                continue

            # Execute the tool
            try:
                if block.name == "search_web":
                    result = search_web(**block.input)
                elif block.name == "search_cache":
                    result = search_cache(**block.input)
                elif block.name == "answer_from_knowledge":
                    result = answer_from_knowledge(**block.input)
                else:
                    result = {"error": f"Unknown tool: {block.name}"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
            except Exception as e:
                error_msg = str(e)
                tracker.record_attempt(block.name, block.input, error=error_msg)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": error_msg}),
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})

    return "Max turns reached."

result = run_agent("What is the best Python web framework in 2024?")
print(result)
# Expected: Agent tries search_web (fails), tries search_cache (succeeds), returns answer
# OR: Agent tries search_web twice (fails both), switches to answer_from_knowledge
```

**Expected Token Savings:** Prevents 5-10 wasted retry turns that each consume 500-2000 tokens of accumulated context.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 2 — Error Classification with Adaptive Retry Strategy

Classify each error and apply the appropriate retry strategy (backoff, alternate tool, abandon).

```python
import anthropic
import json
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Callable

client = anthropic.Anthropic()

class ErrorClass(Enum):
    TRANSIENT = "transient"         # retry same args, maybe with backoff
    RATE_LIMIT = "rate_limit"       # wait, then retry OR switch tool
    NOT_FOUND = "not_found"         # change query/args
    AUTH_ERROR = "auth_error"       # do not retry — report to user
    INVALID_INPUT = "invalid_input" # fix the args before retrying
    QUOTA_EXCEEDED = "quota"        # switch tools or give up
    UNKNOWN = "unknown"             # retry once, then escalate

@dataclass
class RetryStrategy:
    error_class: ErrorClass
    agent_instruction: str
    should_retry: bool
    wait_seconds: float = 0.0
    max_retries: int = 1

ERROR_STRATEGIES: dict[ErrorClass, RetryStrategy] = {
    ErrorClass.TRANSIENT: RetryStrategy(
        ErrorClass.TRANSIENT,
        "This appears to be a transient network error. You may retry once with the same arguments.",
        should_retry=True, max_retries=1, wait_seconds=1.0
    ),
    ErrorClass.RATE_LIMIT: RetryStrategy(
        ErrorClass.RATE_LIMIT,
        "Rate limit hit. Switch to an alternative tool or tell the user you need to wait.",
        should_retry=False
    ),
    ErrorClass.NOT_FOUND: RetryStrategy(
        ErrorClass.NOT_FOUND,
        "The resource was not found. Change your search query or identifier before retrying.",
        should_retry=True, max_retries=2
    ),
    ErrorClass.AUTH_ERROR: RetryStrategy(
        ErrorClass.AUTH_ERROR,
        "Authentication failed. Do not retry — report this to the user.",
        should_retry=False
    ),
    ErrorClass.INVALID_INPUT: RetryStrategy(
        ErrorClass.INVALID_INPUT,
        "Your input was invalid. Fix the argument format or value before retrying.",
        should_retry=True, max_retries=1
    ),
    ErrorClass.QUOTA_EXCEEDED: RetryStrategy(
        ErrorClass.QUOTA_EXCEEDED,
        "Daily quota exceeded. Use a different tool or inform the user.",
        should_retry=False
    ),
}

def classify_error(error_text: str) -> ErrorClass:
    """Classify an error message into an actionable category."""
    error_lower = error_text.lower()

    if any(kw in error_lower for kw in ["429", "rate limit", "too many requests"]):
        return ErrorClass.RATE_LIMIT
    if any(kw in error_lower for kw in ["quota", "limit exceeded", "usage limit"]):
        return ErrorClass.QUOTA_EXCEEDED
    if any(kw in error_lower for kw in ["401", "403", "unauthorized", "forbidden", "invalid api key"]):
        return ErrorClass.AUTH_ERROR
    if any(kw in error_lower for kw in ["404", "not found", "does not exist", "no such"]):
        return ErrorClass.NOT_FOUND
    if any(kw in error_lower for kw in ["400", "invalid", "bad request", "validation", "schema"]):
        return ErrorClass.INVALID_INPUT
    if any(kw in error_lower for kw in ["timeout", "connection", "network", "503", "502"]):
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN

def build_adaptive_error_message(error_text: str, tool_name: str, tool_input: dict) -> str:
    """Build an error message with classification and next-step guidance."""
    error_class = classify_error(error_text)
    strategy = ERROR_STRATEGIES.get(error_class, ERROR_STRATEGIES[ErrorClass.TRANSIENT])

    if strategy.wait_seconds > 0:
        time.sleep(strategy.wait_seconds)

    return json.dumps({
        "error": error_text,
        "error_class": error_class.value,
        "should_retry_same_args": strategy.should_retry,
        "max_retries_allowed": strategy.max_retries,
        "AGENT_INSTRUCTION": strategy.agent_instruction,
        "tool_that_failed": tool_name,
    })

# Simulated tools with different failure modes
def tool_database_query(sql: str) -> dict:
    if "invalid" in sql.lower():
        raise ValueError("400 Bad Request: Invalid SQL syntax near 'invalid'")
    if "DROP" in sql:
        raise PermissionError("403 Forbidden: Only SELECT queries are allowed")
    raise ConnectionError("503 Service Unavailable: Database is temporarily down")

def tool_file_read(path: str) -> dict:
    if not path.startswith("/allowed/"):
        raise FileNotFoundError(f"404 Not Found: {path} does not exist")
    return {"content": f"Contents of {path}"}

tools = [
    {
        "name": "database_query",
        "description": "Run a SQL query",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "file_read",
        "description": "Read a file at a given path",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    },
]

SYSTEM = (
    "You are a data assistant. When tools fail:\n"
    "1. Read the error_class field to understand the failure type\n"
    "2. Follow the AGENT_INSTRUCTION exactly\n"
    "3. If should_retry_same_args=false, CHANGE your approach before trying again\n"
    "4. Never exceed max_retries_allowed for the same tool+arguments"
)

def run_adaptive_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    for _ in range(8):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                try:
                    if block.name == "database_query":
                        result = json.dumps(tool_database_query(**block.input))
                    elif block.name == "file_read":
                        result = json.dumps(tool_file_read(**block.input))
                    else:
                        result = json.dumps({"error": "Unknown tool"})
                except Exception as e:
                    result = build_adaptive_error_message(str(e), block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

    return "Max turns reached."

print(run_adaptive_agent("Read the file at /secret/data.csv and summarize it"))
print()
print(run_adaptive_agent("Query the database: SELECT COUNT(*) FROM users"))
```

**Expected Token Savings:** Error classification eliminates 2-6 identical retries per session; saves 1,000-5,000 tokens in stuck loops.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Explicit Alternative Action Requirement

When a tool fails, require the agent to list alternative approaches before choosing one.

```python
import anthropic
import json

client = anthropic.Anthropic()

ADAPTIVE_SYSTEM = """You are a resourceful assistant. When a tool call fails:

1. DIAGNOSE: What type of failure is this? (rate limit / not found / invalid input / auth / network)
2. LIST ALTERNATIVES: Before retrying, explicitly state 2-3 different approaches you could try
3. CHOOSE: Pick the best alternative and execute it
4. ESCALATE: After 2 failed approaches, tell the user what you tried and what you need from them

Format your diagnosis as:
"Failure analysis: [type]. Alternatives: (a) [alt1], (b) [alt2], (c) [alt3]. Trying: [choice]"

Never retry the exact same tool call without this analysis step."""

def run_with_forced_analysis(query: str, tools: list, tool_dispatcher: dict) -> str:
    messages = [{"role": "user", "content": query}]
    failed_approaches = []

    for turn in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=768,
            system=ADAPTIVE_SYSTEM,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []

        for block in response.content:
            if block.type == "tool_use":
                approach_key = f"{block.name}:{json.dumps(block.input, sort_keys=True)}"

                if approach_key in failed_approaches:
                    # Blocked: same approach already failed
                    result = json.dumps({
                        "BLOCKED": True,
                        "reason": "You already tried this exact call and it failed.",
                        "failed_approaches": failed_approaches,
                        "instruction": (
                            "You must try a DIFFERENT tool or DIFFERENT arguments. "
                            "If you have no more alternatives, explain to the user what you tried."
                        )
                    })
                else:
                    fn = tool_dispatcher.get(block.name)
                    if fn:
                        try:
                            output = fn(**block.input)
                            result = json.dumps({"success": True, "data": output})
                        except Exception as e:
                            failed_approaches.append(approach_key)
                            result = json.dumps({
                                "success": False,
                                "error": str(e),
                                "failed_approaches_count": len(failed_approaches),
                                "instruction": ADAPTIVE_SYSTEM.split("Format")[0],
                            })
                    else:
                        result = json.dumps({"error": f"Unknown tool: {block.name}"})

                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": results})

    return "Max turns reached — agent may be stuck."

# Demo with multiple tools having partial failures
tools = [
    {"name": "search_primary", "description": "Primary search engine",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "search_backup", "description": "Backup search engine",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "knowledge_lookup", "description": "Internal knowledge base",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
]

call_counts = {}
def make_failing_search(name, fail_count=2):
    def tool(**kwargs):
        key = name
        call_counts[key] = call_counts.get(key, 0) + 1
        if call_counts[key] <= fail_count:
            raise ConnectionError(f"503: {name} temporarily unavailable")
        return {"results": [f"Found results for {list(kwargs.values())[0]}"][:1]}
    return tool

dispatcher = {
    "search_primary": make_failing_search("search_primary", fail_count=3),
    "search_backup": make_failing_search("search_backup", fail_count=1),
    "knowledge_lookup": lambda topic: {"answer": f"Knowledge base says: {topic} is well-documented."},
}

result = run_with_forced_analysis(
    "Search for recent news about Python 3.13",
    tools, dispatcher
)
print(result)
```

**Expected Token Savings:** Explicit analysis step adds ~100 tokens per failure but prevents 5-8 silent identical retries (~3,000 tokens). Net savings: ~2,500 tokens per stuck loop.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 4 — Exponential Backoff with Jitter for Transient Errors

Implement proper backoff for errors that warrant retry, preventing thundering-herd retries.

```python
import anthropic
import asyncio
import json
import random
import time
from typing import Optional

client = anthropic.AsyncAnthropic()

async def exponential_backoff_execute(
    tool_fn,
    tool_kwargs: dict,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.3,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Execute a tool with exponential backoff + jitter.
    Returns (result, None) on success, (None, error_msg) after all retries.
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = await tool_fn(**tool_kwargs)
            if attempt > 0:
                print(f"  Succeeded after {attempt} retries")
            return result, None

        except Exception as e:
            last_error = str(e)
            error_lower = last_error.lower()

            # Don't retry on non-transient errors
            if any(kw in error_lower for kw in ["401", "403", "404", "400", "invalid"]):
                print(f"  Non-retryable error: {last_error}")
                return None, f"NON_RETRYABLE: {last_error}"

            if attempt >= max_retries:
                break

            # Exponential backoff with jitter
            delay = min(base_delay * (2 ** attempt), max_delay)
            delay *= (1 + random.uniform(-jitter, jitter))
            print(f"  Attempt {attempt + 1} failed ({last_error[:60]}). Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

    return None, f"EXHAUSTED_RETRIES ({max_retries}): {last_error}"

async def tool_unstable_api(query: str) -> dict:
    """Simulates a flaky API that fails 60% of the time transiently."""
    if random.random() < 0.6:
        raise ConnectionError("503 Service Unavailable")
    return {"result": f"API result for: {query}"}

async def tool_stable_fallback(query: str) -> dict:
    return {"result": f"Cached result for: {query}"}

tools = [
    {
        "name": "call_api",
        "description": "Call the primary API (may be flaky)",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "use_fallback",
        "description": "Use cached fallback when API is unavailable",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
]

async def run_agent_with_backoff(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(6):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            tools=tools,
            messages=messages,
            system="When call_api fails after retries, automatically use_fallback instead.",
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "call_api":
                    data, error = await exponential_backoff_execute(
                        tool_unstable_api, block.input,
                        max_retries=3, base_delay=0.5
                    )
                elif block.name == "use_fallback":
                    data, error = await tool_stable_fallback(**block.input), None
                    if isinstance(data, dict):
                        data, error = data, None
                else:
                    data, error = None, f"Unknown tool: {block.name}"

                content = json.dumps(data) if data else json.dumps({"error": error})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})

        messages.append({"role": "user", "content": results})

    return "Max turns reached."

result = asyncio.run(run_agent_with_backoff("Look up data about pandas DataFrame"))
print(result)
```

**Expected Token Savings:** Proper backoff prevents immediate identical retries, reducing turn count from 8 to 2-3 for transient errors.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

### Option 5 — Turn-Based Action Diversity Enforcer

Track actions per turn and block the agent from repeating any action within the same session.

```python
import anthropic
import json
from collections import defaultdict

client = anthropic.Anthropic()

class ActionDiversityEnforcer:
    """Prevents the agent from repeating identical tool calls."""

    def __init__(self, max_per_call: int = 2):
        self.max_per_call = max_per_call
        self.call_log: dict[str, list[dict]] = defaultdict(list)
        self.blocked_count = 0

    def _signature(self, tool_name: str, tool_input: dict) -> str:
        return f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"

    def check(self, tool_name: str, tool_input: dict) -> tuple[bool, str]:
        """Check if this call is allowed. Returns (allowed, message)."""
        sig = self._signature(tool_name, tool_input)
        history = self.call_log[sig]

        if len(history) >= self.max_per_call:
            self.blocked_count += 1
            prior_errors = [h.get("result_summary", "unknown") for h in history]
            return False, json.dumps({
                "DIVERSITY_BLOCK": True,
                "attempts_so_far": len(history),
                "prior_results": prior_errors,
                "instruction": (
                    f"You have called {tool_name} with these arguments {len(history)} time(s) already. "
                    f"This is not working. You MUST try one of: "
                    f"(1) Different arguments for {tool_name}, "
                    f"(2) A completely different tool, "
                    f"(3) Tell the user what you've tried and what you need to proceed."
                )
            })

        return True, ""

    def record_result(self, tool_name: str, tool_input: dict, result_summary: str):
        sig = self._signature(tool_name, tool_input)
        self.call_log[sig].append({"result_summary": result_summary})

enforcer = ActionDiversityEnforcer(max_per_call=2)

# Simulated tool that always fails
def always_failing_search(q: str) -> dict:
    raise RuntimeError("Search service is down")

def knowledge_base_lookup(topic: str) -> dict:
    return {"facts": [f"{topic} is a well-known subject in computer science"]}

tools = [
    {
        "name": "search",
        "description": "Web search",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    },
    {
        "name": "knowledge_lookup",
        "description": "Look up from internal knowledge base",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    },
]

def dispatch(name: str, input_: dict) -> str:
    try:
        if name == "search":
            return json.dumps(always_failing_search(**input_))
        if name == "knowledge_lookup":
            return json.dumps(knowledge_base_lookup(**input_))
    except Exception as e:
        return json.dumps({"error": str(e)})

def run(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=tools, messages=messages,
            system="Research assistant. When one approach fails, try a different tool or approach."
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                allowed, block_msg = enforcer.check(block.name, block.input)
                if not allowed:
                    content = block_msg
                else:
                    content = dispatch(block.name, block.input)
                    enforcer.record_result(block.name, block.input, content[:100])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "user", "content": results})
    return "Max turns."

print(run("Tell me about machine learning"))
print(f"\nBlocked identical calls: {enforcer.blocked_count}")
```

**Expected Token Savings:** Blocking identical calls after 2 attempts saves 3-8 turns (1,500-4,000 tokens) in stuck loops.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 6 — Retry Plan Generation Before Execution

Before retrying after failure, require the agent to generate a written retry plan and get approval.

```python
import anthropic
import json

client = anthropic.Anthropic()

RETRY_PLAN_TOOL = {
    "name": "propose_retry_plan",
    "description": (
        "When the previous tool call failed, call this FIRST to propose your retry plan. "
        "Do not call any other tool until you have proposed a plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "failed_tool": {"type": "string"},
            "error_analysis": {"type": "string", "description": "Why did it fail?"},
            "what_changes": {"type": "string", "description": "What will you do differently?"},
            "alternative_if_retry_fails": {"type": "string", "description": "What will you do if this retry also fails?"},
        },
        "required": ["failed_tool", "error_analysis", "what_changes", "alternative_if_retry_fails"]
    }
}

def evaluate_retry_plan(plan: dict) -> str:
    """Evaluate whether the retry plan actually changes the approach."""
    changes = plan.get("what_changes", "").lower()
    same_indicators = ["same", "identical", "again", "retry exactly", "same args"]

    if any(indicator in changes for indicator in same_indicators):
        return json.dumps({
            "plan_approved": False,
            "reason": (
                "Your plan doesn't describe a meaningful change. "
                "You must use different arguments, a different tool, or a different approach. "
                "What SPECIFICALLY will be different?"
            )
        })

    return json.dumps({
        "plan_approved": True,
        "proceed": "Your retry plan has been approved. Execute your planned approach now.",
        "reminder": plan.get("alternative_if_retry_fails", "")
    })

tools = [
    RETRY_PLAN_TOOL,
    {
        "name": "fetch_data",
        "description": "Fetch data from an endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["endpoint"]
        }
    }
]

call_count = [0]
def fetch_data(endpoint: str, params: dict = None) -> dict:
    call_count[0] += 1
    if call_count[0] <= 2:
        raise ValueError(f"404: Endpoint {endpoint} not found")
    return {"data": "success", "endpoint": endpoint}

SYSTEM = """When a tool call fails:
1. FIRST call propose_retry_plan to describe what you'll do differently
2. Only after the plan is approved, make your next tool call
3. If you retry with no change, your plan will be rejected"""

messages = [{"role": "user", "content": "Fetch data from /api/users with limit=10"}]

for _ in range(10):
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512, tools=tools,
        system=SYSTEM, messages=messages
    )
    if resp.stop_reason == "end_turn":
        print(resp.content[0].text)
        break
    messages.append({"role": "assistant", "content": resp.content})
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            if block.name == "propose_retry_plan":
                content = evaluate_retry_plan(block.input)
            elif block.name == "fetch_data":
                try:
                    content = json.dumps(fetch_data(**block.input))
                except Exception as e:
                    content = json.dumps({"error": str(e)})
            else:
                content = json.dumps({"error": "unknown"})
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Retry plan requirement adds ~150 tokens overhead but eliminates 4-8 identical retry turns (~2,000-4,000 tokens).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

## Comparison

| Option | Prevention | Auto-Adapts | Requires Planning | Complexity |
|--------|-----------|-------------|------------------|------------|
| 1 — Retry Budget | Hard block after N | Yes (blocked) | No | Low |
| 2 — Error Classification | Strategy-based | Yes | No | Medium |
| 3 — Alternative Requirement | Narrative-forced | Yes | Yes | Low |
| 4 — Exponential Backoff | Delay-based | No | No | Low |
| 5 — Diversity Enforcer | Per-signature block | Yes (blocked) | No | Medium |
| 6 — Plan Generation | Approval-gated | Yes | Yes | Medium |

**Start with Option 1** (retry budget) — it's a simple wrapper that catches all identical-retry patterns. Add **Option 2** (error classification) to route different errors appropriately. Use **Option 4** (backoff) specifically for transient network failures.
