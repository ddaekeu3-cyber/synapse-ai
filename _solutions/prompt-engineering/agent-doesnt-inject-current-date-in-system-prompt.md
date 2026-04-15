---
layout: solution
title: "Agent doesn't inject current date in system prompt"
category: prompt-engineering
description: "Agent has no date in its system prompt, causing temporal reasoning failures: it answers time-sensitive questions with stale training knowledge, miscalculates deadlines, and formats dates inconsistently."
tags: [prompt-engineering, system-prompt, temporal-reasoning, date, time]
---

## Symptom

The agent confidently answers "what day is it?" with a date from its training window, calculates "in 30 days" from an imaginary anchor, or formats ISO timestamps in the wrong timezone. When asked to schedule tasks, it reasons from a wrong starting point and produces deadlines that are already past.

## Root Cause

Large language models have no real-time clock. Their training cutoff is the last date they know with certainty. Without an explicit `current_date` in the system prompt, the model guesses — and guesses wrong. This is especially damaging for scheduling agents, news summarizers, deadline trackers, and any tool that reasons about "now", "yesterday", "next week", or "recently".

## Fix

Inject the current date (and timezone when relevant) into the system prompt on every request. Keep the format unambiguous (ISO 8601) and supplement it with derived values (day of week, UTC offset) that the model commonly needs.

---

### Option 1 — Minimal ISO 8601 date injection

```python
import anthropic
from datetime import date

client = anthropic.Anthropic(api_key="sk-live-...")

BASE_SYSTEM_PROMPT = """You are a helpful assistant.
You help users manage tasks, deadlines, and scheduling."""


def make_system_prompt() -> str:
    today = date.today().isoformat()  # e.g. "2026-04-15"
    return f"{BASE_SYSTEM_PROMPT}\n\nToday's date: {today}"


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=make_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None — adds ~10 tokens; prevents entire hallucinated reasoning chains about incorrect dates.
**Environment:** Any agent that answers time-relative questions; the minimal viable fix.

---

### Option 2 — Full temporal context block (date, time, timezone, day-of-week)

```python
import anthropic
from datetime import datetime
import zoneinfo

client = anthropic.Anthropic(api_key="sk-live-...")

BASE_SYSTEM = "You are a scheduling assistant that helps users plan tasks and meetings."


def temporal_block(tz_name: str = "UTC") -> str:
    tz = zoneinfo.ZoneInfo(tz_name)
    now = datetime.now(tz)
    return (
        f"Current date and time:\n"
        f"  Date:       {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})\n"
        f"  Time:       {now.strftime('%H:%M:%S')}\n"
        f"  Timezone:   {tz_name} (UTC{now.strftime('%z')})\n"
        f"  ISO 8601:   {now.isoformat()}\n"
        f"  Week:       ISO week {now.isocalendar().week} of {now.year}\n"
    )


def run_agent(user_message: str, user_tz: str = "America/New_York") -> str:
    system = f"{BASE_SYSTEM}\n\n{temporal_block(user_tz)}"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Adds ~60 tokens; eliminates multi-turn clarification exchanges about timezone and date format.
**Environment:** Scheduling, calendar, or meeting agents where the user's local timezone matters for correctness.

---

### Option 3 — Per-user timezone from profile, cached system prompt prefix

```python
import anthropic
from datetime import datetime
import zoneinfo
from functools import lru_cache

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated user profile store
USER_PROFILES: dict[str, dict] = {
    "user_001": {"name": "Alice", "timezone": "America/Los_Angeles"},
    "user_002": {"name": "Bob", "timezone": "Europe/London"},
    "user_003": {"name": "Carol", "timezone": "Asia/Tokyo"},
}

STATIC_SYSTEM = (
    "You are a personal assistant. Always reason about dates using the user's "
    "local timezone provided in the context header. Never assume UTC unless stated."
)


def date_header(user_id: str) -> str:
    profile = USER_PROFILES.get(user_id, {"timezone": "UTC"})
    tz = zoneinfo.ZoneInfo(profile["timezone"])
    now = datetime.now(tz)
    return (
        f"[User context]\n"
        f"User: {profile.get('name', user_id)}\n"
        f"Local date: {now.strftime('%Y-%m-%d (%A)')}\n"
        f"Local time: {now.strftime('%H:%M')} {profile['timezone']} "
        f"(UTC{now.strftime('%z')})\n"
    )


def run_agent(user_id: str, user_message: str) -> str:
    system = f"{STATIC_SYSTEM}\n\n{date_header(user_id)}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


if __name__ == "__main__":
    print(run_agent("user_001", "What time should I schedule a meeting for 3pm Tokyo time?"))
```

**Expected Token Savings:** None on tokens, but eliminates timezone conversion errors that cause costly follow-up turns.
**Environment:** Multi-user assistants where each user has a stored timezone preference.

---

### Option 4 — Async agent with date injected per-turn in multi-turn conversations

```python
import anthropic
import asyncio
from datetime import datetime, timezone

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

SYSTEM_TEMPLATE = """\
You are a helpful task management assistant.

{date_block}

When the user mentions relative dates ("tomorrow", "next Monday", "in two weeks"),
always resolve them to absolute ISO dates before acting.
"""


def current_date_block() -> str:
    now = datetime.now(timezone.utc)
    return (
        f"Current UTC datetime: {now.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"Day of week: {now.strftime('%A')}"
    )


async def chat_session(initial_message: str) -> None:
    messages: list[dict] = []
    print(f"You: {initial_message}")

    for turn_input in [initial_message, "And the week after that?", "exit"]:
        if turn_input == "exit":
            break

        # Re-inject date on every turn so long sessions stay accurate
        system = SYSTEM_TEMPLATE.format(date_block=current_date_block())
        messages.append({"role": "user", "content": turn_input})

        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )

        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"Agent: {reply}\n")


if __name__ == "__main__":
    asyncio.run(chat_session("Schedule a code review for next Thursday."))
```

**Expected Token Savings:** The system prompt is regenerated each turn (not cached), but this ensures the date is never stale in long-running sessions.
**Environment:** Long-lived async conversations where time may pass between turns; trades cache hits for accuracy.

---

### Option 5 — Date injection via tool result (agent always calls `get_current_date` first)

```python
import anthropic
from datetime import datetime, timezone

client = anthropic.Anthropic(api_key="sk-live-...")

DATE_TOOL = {
    "name": "get_current_date",
    "description": (
        "Returns the current date, time, and day of week in UTC. "
        "Call this before answering any question that involves dates, "
        "deadlines, scheduling, or time-relative language."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. 'America/New_York'. Defaults to UTC.",
            }
        },
        "required": [],
    },
}

SYSTEM = (
    "You are a scheduling assistant. "
    "Always call get_current_date before answering questions about time, dates, or deadlines. "
    "Never guess the current date from your training data."
)


def handle_get_current_date(timezone_name: str = "UTC") -> str:
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    return (
        f"date: {now.strftime('%Y-%m-%d')}\n"
        f"time: {now.strftime('%H:%M:%S')}\n"
        f"day_of_week: {now.strftime('%A')}\n"
        f"iso8601: {now.isoformat()}\n"
        f"timezone: {timezone_name}"
    )


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=[DATE_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "get_current_date":
                    tz = block.input.get("timezone", "UTC")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": handle_get_current_date(tz),
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** Adds one tool round-trip; eliminates hallucinated date reasoning that can cascade into incorrect multi-step plans.
**Environment:** Agents where you want an explicit, auditable record that the model retrieved the date rather than guessed it.

---

### Option 6 — Prompt-cached system prompt with daily-rotating date header

```python
import anthropic
from datetime import date

client = anthropic.Anthropic(api_key="sk-live-...")

# This large block is stable for the entire day → gets cached after the first call
LARGE_STATIC_CONTEXT = """
You are a comprehensive business assistant with expertise in scheduling,
project management, financial analysis, and strategic planning.

Guidelines:
- Always confirm deadlines before committing to action
- Use ISO 8601 dates (YYYY-MM-DD) in all outputs
- When calculating durations, account for weekends and public holidays
- Flag any date that falls on a weekend or known holiday
- For multi-timezone meetings, list all participant local times

Output format for scheduled items:
  Task: <name>
  Due: <YYYY-MM-DD> (<day of week>)
  Priority: <high|medium|low>
  Notes: <any caveats>
""".strip()


def build_system_prompt() -> str:
    today = date.today().isoformat()
    day_name = date.today().strftime("%A")
    # Date header changes daily; static context is cacheable within the day
    date_line = f"[CURRENT DATE: {today} ({day_name})]"
    return f"{date_line}\n\n{LARGE_STATIC_CONTEXT}"


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=build_system_prompt(),
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": user_message}],
    )
    usage = response.usage
    print(
        f"Tokens — input: {usage.input_tokens}, "
        f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}, "
        f"cache_write: {getattr(usage, 'cache_creation_input_tokens', 0)}"
    )
    return response.content[0].text


# Comparison table
# | Option | Date Source | Timezone Support | Cache-Friendly |
# |--------|------------|-----------------|----------------|
# | 1 Minimal ISO | date.today() | UTC only | Yes |
# | 2 Full temporal block | datetime.now(tz) | Per-call | No (changes each call) |
# | 3 Per-user profile | datetime.now(tz) | Per-user | Partial |
# | 4 Async per-turn | datetime.now(utc) | UTC | No |
# | 5 Tool-based | Tool result | Per-tool-call | Yes (system cached) |
# | 6 Daily-rotating cache | date.today() | UTC | Yes (daily cache hit) |
```

**Expected Token Savings:** The large static context block is written to cache on day 1 and read from cache on all subsequent calls that day; only the small date header changes.
**Environment:** High-volume agents with large static system prompts where prompt caching ROI is significant.
