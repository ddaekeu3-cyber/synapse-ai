---
layout: solution
title: "Agent hallucinates dates and timestamps"
category: hallucination
description: "Agent generates plausible but wrong dates: uses the training cutoff year instead of the current year, calculates 'next Tuesday' relative to an assumed date, confuses time zones, or invents specific dates for events that only have approximate timing. Injecting the current date and using explicit date tools prevents these errors."
tags: [hallucination, dates, timestamps, timezone, temporal, prompt-engineering, tools]
---

## Symptom

User asks "when is the next quarterly review?" and the agent confidently answers with a date in 2024 — its training cutoff year — instead of 2026. Or the user asks "what's today's date?" and the agent answers "I don't have access to the current date" even though the date was injected in the system prompt. Or the agent generates a deadline of "March 15" without specifying the year, causing confusion when the date is parsed by downstream systems.

## Root Cause

Language models have a training cutoff and no real-time clock. Without explicit current date injection, the model estimates "now" from statistical priors in its training data — typically landing near the cutoff date. For relative date calculations ("two weeks from now", "next Monday"), the model needs an anchor date. Without it, calculations are anchored to the wrong year. Time zone handling is especially error-prone because the model lacks system context.

## Fix

Inject the current date and time zone in the system prompt at startup. For calculations involving relative dates, provide an explicit anchor. For any date-sensitive output that will be parsed downstream, require ISO 8601 format with year.

---

### Option 1 — Inject current date in system prompt at every session start

```python
import anthropic
from datetime import datetime, timezone

client = anthropic.Anthropic(api_key="sk-live-...")


def get_date_context(tz_name: str = "UTC") -> str:
    """
    Generate a current date/time block for injection into the system prompt.
    Called fresh on each session to ensure accuracy.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    return (
        f"Current date and time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"({now.strftime('%A, %B %d, %Y')})\n"
        f"Day of week: {now.strftime('%A')}\n"
        f"Week number: {now.strftime('%W')} of {now.year}\n"
        f"Time zone: {tz_name}"
    )


def build_system_prompt(base_prompt: str, user_timezone: str = "UTC") -> str:
    date_block = get_date_context(user_timezone)
    return f"{base_prompt}\n\n## Current date context (authoritative):\n{date_block}\n"


def run_agent(user_message: str, user_timezone: str = "America/New_York") -> str:
    system = build_system_prompt(
        "You are a helpful scheduling assistant.",
        user_timezone,
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test: without date injection, model uses training cutoff year
# With injection, model uses actual current date

# Example: "What day is three weeks from today?"
result = run_agent("What's the date exactly three weeks from today?")
print(result)

# Example: "Is Q3 over yet?" — needs current date to answer correctly
result2 = run_agent("Is Q3 of this year over yet?")
print(result2)
```

**Expected Token Savings:** Date injection adds ~80 tokens per session; prevents 1–2 correction turns (~400–800 tokens each) when the user corrects wrong dates; net savings on any date-sensitive task.
**Environment:** Any agent answering date-relative questions; date injection is the single most impactful fix for temporal hallucinations — without it, all "when is..." and "how long until..." answers are anchored to the wrong year.

---

### Option 2 — Date calculation tool to prevent arithmetic errors

```python
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Provide a date calculation tool to prevent the model from doing date math
DATE_TOOLS = [
    {
        "name": "calculate_date",
        "description": (
            "Calculate a date relative to today or a given date. "
            "Use this for ANY date arithmetic — do not calculate dates yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base_date": {
                    "type": "string",
                    "description": "ISO date string (YYYY-MM-DD) or 'today'",
                },
                "offset_days": {
                    "type": "integer",
                    "description": "Number of days to add (positive) or subtract (negative)",
                },
                "target_weekday": {
                    "type": "string",
                    "description": "Optional: target day of week (monday, tuesday, ..., sunday)",
                    "enum": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
                },
            },
            "required": ["base_date", "offset_days"],
        },
    },
    {
        "name": "format_date",
        "description": "Format a date string into a specified format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date string (YYYY-MM-DD)"},
                "format": {
                    "type": "string",
                    "description": "Output format: 'iso', 'long', 'short', 'relative'",
                    "enum": ["iso", "long", "short", "relative"],
                },
                "timezone": {"type": "string", "description": "IANA timezone name"},
            },
            "required": ["date", "format"],
        },
    },
]


def handle_calculate_date(base_date: str, offset_days: int, target_weekday: str | None = None) -> str:
    today = datetime.now().date()
    if base_date == "today":
        start = today
    else:
        start = datetime.fromisoformat(base_date).date()

    result = start + timedelta(days=offset_days)

    if target_weekday:
        day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
        target_day = day_map.get(target_weekday.lower(), 0)
        days_ahead = (target_day - result.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7   # next occurrence, not today
        result = result + timedelta(days=days_ahead)

    return json.dumps({
        "date": result.isoformat(),
        "day_of_week": result.strftime("%A"),
        "formatted": result.strftime("%B %d, %Y"),
        "days_from_today": (result - today).days,
    })


def handle_format_date(date: str, format: str, timezone: str = "UTC") -> str:
    dt = datetime.fromisoformat(date)
    today = datetime.now().date()
    delta = (dt.date() - today).days

    formats = {
        "iso": dt.strftime("%Y-%m-%d"),
        "long": dt.strftime("%A, %B %d, %Y"),
        "short": dt.strftime("%b %d, %Y"),
        "relative": (
            "today" if delta == 0 else
            "yesterday" if delta == -1 else
            "tomorrow" if delta == 1 else
            f"in {delta} days" if delta > 0 else
            f"{-delta} days ago"
        ),
    }
    return json.dumps({"result": formats.get(format, dt.isoformat())})


def run_date_agent(user_message: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    system = (
        f"You are a scheduling assistant. Today is {today}.\n"
        "For ANY date calculation (days from now, next Monday, etc.), "
        "ALWAYS use the calculate_date tool — never calculate dates yourself."
    )
    messages = [{"role": "user", "content": user_message}]

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=DATE_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "calculate_date":
                        result = handle_calculate_date(**block.input)
                    elif block.name == "format_date":
                        result = handle_format_date(**block.input)
                    else:
                        result = '{"error": "unknown tool"}'
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


print(run_date_agent("What date is 6 weeks from today, and what day of the week is that?"))
print(run_date_agent("When is next Friday?"))
```

**Expected Token Savings:** Date calculation tool costs ~200 tokens per use; prevents wrong-year arithmetic errors that would require a correction turn (~400 tokens); net positive for any relative date calculation.
**Environment:** Scheduling, deadline, and calendar agents; the `calculate_date` tool is especially important for "next [weekday]" and "N weeks from now" calculations which the model frequently gets wrong.

---

### Option 3 — Time zone aware date injection per user

```python
import anthropic
from datetime import datetime
from zoneinfo import ZoneInfo
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# User profile with time zone
USER_TIMEZONES = {
    "alice": "America/New_York",
    "bob": "Europe/London",
    "carol": "Asia/Tokyo",
}


def get_user_time_context(user_id: str) -> dict:
    """Build a full time context for a specific user."""
    tz_name = USER_TIMEZONES.get(user_id, "UTC")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    # Also compute UTC for cross-timezone references
    utc_now = datetime.now(ZoneInfo("UTC"))

    return {
        "user_id": user_id,
        "timezone": tz_name,
        "local_time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "local_date": now.strftime("%Y-%m-%d"),
        "day_of_week": now.strftime("%A"),
        "utc_offset": now.strftime("%z"),
        "utc_time": utc_now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "business_hours": "09:00-17:00" + tz_name,   # assume standard hours
        "is_business_hours": 9 <= now.hour < 17 and now.weekday() < 5,
    }


def run_agent_with_user_tz(user_id: str, user_message: str) -> str:
    ctx = get_user_time_context(user_id)
    system = (
        f"You are a scheduling assistant.\n\n"
        f"User context:\n"
        f"- Local time: {ctx['local_time']}\n"
        f"- Time zone: {ctx['timezone']} (UTC{ctx['utc_offset']})\n"
        f"- It is currently {'a weekday' if ctx['is_business_hours'] else 'outside business hours'}.\n\n"
        "When giving dates, always include the year and be specific. "
        "When mentioning times, always specify the time zone."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test across time zones — Alice in NYC, Bob in London, Carol in Tokyo
for user_id in ["alice", "bob", "carol"]:
    print(f"\n[{user_id}]")
    result = run_agent_with_user_tz(user_id, "Schedule a meeting for 3pm tomorrow")
    print(result[:200])
```

**Expected Token Savings:** Per-user timezone context adds ~100 tokens; prevents timezone-confusion errors (scheduling a meeting at 3pm UTC when the user means 3pm EST) that would require 1–2 clarification turns (~600 tokens).
**Environment:** Multi-user scheduling agents; per-user timezone injection is essential when users are in different time zones and the agent needs to give locally-correct answers.

---

### Option 4 — ISO 8601 enforcement for downstream systems

```python
import anthropic
import re
from datetime import datetime

client = anthropic.Anthropic(api_key="sk-live-...")

ISO_8601_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?')
AMBIGUOUS_DATE_PATTERN = re.compile(
    r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}'
    r'(?!\s*,?\s*\d{4})'  # not followed by year
)


def detect_ambiguous_dates(text: str) -> list[str]:
    """Find dates in text that lack a year."""
    return AMBIGUOUS_DATE_PATTERN.findall(text)


def validate_date_output(text: str) -> tuple[bool, list[str]]:
    """Check if all dates in the output include a year."""
    issues = []
    ambiguous = detect_ambiguous_dates(text)
    if ambiguous:
        issues.extend(f"Date without year: '{d}'" for d in ambiguous)
    return len(issues) == 0, issues


def run_agent_iso_enforced(user_message: str) -> str:
    today = datetime.now()
    system = (
        f"You are a scheduling assistant. Today is {today.strftime('%Y-%m-%d')}.\n\n"
        "DATE FORMAT RULES:\n"
        "1. Always include the year when mentioning dates (e.g., 'March 15, 2026' not 'March 15')\n"
        "2. For machine-readable outputs, use ISO 8601: YYYY-MM-DD\n"
        "3. For deadlines and appointments, include time zone: '2026-03-15T14:00:00-05:00'\n"
        "4. Never use ambiguous formats like '3/15' or '15/3'\n"
    )

    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        output = response.content[0].text
        valid, issues = validate_date_output(output)

        if valid:
            return output

        print(f"[DateValidation] Issues found: {issues}")
        if attempt == 0:
            # Add correction instruction
            user_message = (
                f"{user_message}\n\n"
                f"IMPORTANT: Your previous response had date issues: {'; '.join(issues)}. "
                "Please include the year for all dates."
            )

    return output   # return last attempt


result = run_agent_iso_enforced(
    "Create a project timeline with quarterly milestones for the rest of the year"
)
print(result)
```

**Expected Token Savings:** ISO enforcement adds ~100 tokens of system prompt; post-generation validation catches ambiguous dates before they reach downstream systems — prevents data entry errors that could corrupt calendars or task management tools.
**Environment:** Agents producing dates for downstream consumption (calendar APIs, project management tools, databases); ISO 8601 validation is essential when dates are parsed programmatically.

---

### Option 5 — Temporal grounding for historical vs current questions

```python
import anthropic
from datetime import datetime

client = anthropic.Anthropic(api_key="sk-live-...")

TEMPORAL_CLASSIFIER_SYSTEM = (
    "Classify this question by temporal scope. Reply with exactly one of:\n"
    "  HISTORICAL — asks about past events, history, or unchanging facts\n"
    "  CURRENT — asks about the present state, current date/time, or recent events\n"
    "  FUTURE — asks about upcoming events, scheduling, or predictions\n"
    "  TIMELESS — facts that don't change over time (math, definitions, etc.)"
)


def classify_temporal_scope(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        system=TEMPORAL_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    label = response.content[0].text.strip().upper()
    valid = {"HISTORICAL", "CURRENT", "FUTURE", "TIMELESS"}
    return label if label in valid else "CURRENT"


def build_temporal_system(scope: str, user_timezone: str = "UTC") -> str:
    now = datetime.now()
    base = "You are a knowledgeable assistant."

    if scope == "TIMELESS":
        return base  # no date context needed

    if scope == "HISTORICAL":
        return (
            f"{base}\n\nNote: Today is {now.strftime('%Y-%m-%d')}. "
            "This question is about historical facts — be precise about dates and avoid "
            "conflating different time periods."
        )

    # CURRENT or FUTURE — full date context
    return (
        f"{base}\n\nCurrent date/time: {now.strftime('%Y-%m-%d %H:%M')} ({user_timezone}). "
        "Use this as your temporal anchor for all date calculations. "
        "Explicitly state the year for all future dates."
    )


def run_agent_temporal_grounded(user_message: str, user_timezone: str = "UTC") -> str:
    scope = classify_temporal_scope(user_message)
    print(f"[Temporal] Scope: {scope}")

    system = build_temporal_system(scope, user_timezone)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test different temporal scopes
questions = [
    "When was the Eiffel Tower built?",       # HISTORICAL — no date injection needed
    "What day of the week is it today?",      # CURRENT — needs date injection
    "When is Easter next year?",              # FUTURE — needs date injection + year
    "What is the Pythagorean theorem?",       # TIMELESS — no date injection
]
for q in questions:
    print(f"\nQ: {q}")
    print(run_agent_temporal_grounded(q)[:150])
```

**Expected Token Savings:** Haiku classifier costs ~20 tokens; skipping date injection for HISTORICAL and TIMELESS questions saves ~80 tokens per question (~40% of questions); date injection only fires when actually needed, optimizing the trade-off.
**Environment:** General-purpose assistants with mixed temporal questions; selective injection avoids bloating context for questions that don't need temporal grounding.

---

### Option 6 — Date hallucination auditor for post-generation verification

```python
import anthropic
import re
from datetime import datetime

client = anthropic.Anthropic(api_key="sk-live-...")

DATE_AUDIT_SYSTEM = (
    "Audit this text for temporal accuracy. Today is {today}.\n"
    "Flag these issues:\n"
    "1. WRONG_YEAR — mentions a year that seems to be the wrong year for the context\n"
    "2. MISSING_YEAR — mentions a date without a year\n"
    "3. IMPOSSIBLE_DATE — a date that cannot be correct (e.g., Feb 30)\n"
    "4. TIMEZONE_MISSING — mentions a time without a timezone\n"
    "5. RELATIVE_UNANCHORED — uses relative time ('next week') without an anchor\n\n"
    "Reply with JSON: {{\"issues\": [{{\"type\": \"...\", \"text\": \"...\", \"line\": ...}}], "
    "\"clean\": true/false}}"
)


def audit_dates_in_response(text: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=DATE_AUDIT_SYSTEM.format(today=today),
        messages=[{"role": "user", "content": text[:2000]}],
    )
    import json
    try:
        return json.loads(response.content[0].text.strip())
    except json.JSONDecodeError:
        return {"issues": [], "clean": True}


def run_agent_with_date_audit(user_message: str) -> str:
    today = datetime.now()
    system = (
        f"You are a scheduling assistant. Today is {today.strftime('%Y-%m-%d (%A)')}. "
        "Always include the year for dates and the timezone for times."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text

    # Audit the output for date issues
    audit = audit_dates_in_response(output)

    if not audit.get("clean", True) and audit.get("issues"):
        print(f"[DateAudit] Found {len(audit['issues'])} issue(s):")
        for issue in audit["issues"]:
            print(f"  [{issue['type']}] {issue.get('text', '')[:60]}")

        # Re-generate with specific corrections
        issue_list = "; ".join(
            f"{i['type']}: {i.get('text','')[:40]}"
            for i in audit["issues"]
        )
        corrected = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": output},
                {"role": "user", "content": f"Please fix these date issues: {issue_list}"},
            ],
        )
        return corrected.content[0].text

    # Comparison table
    # | Option | Prevention | Scope | Cost |
    # |--------|-----------|-------|------|
    # | 1 Date injection | System prompt | All dates | ~80 tok |
    # | 2 Date calc tool | Force tool use | Relative dates | ~200 tok/use |
    # | 3 Per-user timezone | User profile | Timezone errors | ~100 tok |
    # | 4 ISO enforcement | Format rules | Format errors | ~100 tok |
    # | 5 Temporal grounding | Scope routing | Unnecessary injection | ~20 tok |
    # | 6 Post-gen audit | Output validation | All date errors | ~50 tok |

    return output


result = run_agent_with_date_audit(
    "Create a sprint schedule for the next 6 weeks with key milestones"
)
print(result[:400])
```

**Expected Token Savings:** Haiku date audit costs ~50 tokens; catches subtle date errors (wrong year, missing timezone) that would silently propagate to downstream systems; correction retry costs ~1000 tokens but is vastly cheaper than debugging date-corrupted production data.
**Environment:** Agents producing dates for calendar or scheduling systems; post-generation audit is the last line of defense for high-stakes date outputs.
