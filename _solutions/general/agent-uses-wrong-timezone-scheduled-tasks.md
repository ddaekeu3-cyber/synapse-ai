---
layout: solution
title: "Agent Uses Wrong Timezone in Scheduled Tasks — Jobs Fire at Wrong Time"
category: general
description: "Agent schedules a task for '9 AM' but runs it at wrong time because it assumes UTC while user is in a different timezone. Cron jobs, scheduled messages, and reminders fire hours off."
tags: [timezone, scheduling, utc, cron, datetime, localization]
---

# Agent Uses Wrong Timezone in Scheduled Tasks — Jobs Fire at Wrong Time

## Symptom
- "Send report at 9 AM" → report arrives at 2 AM or 5 PM
- Scheduled cron job fires at wrong local time
- Agent says "I'll remind you at 3 PM" — reminder arrives 5 hours late
- Works correctly in one region, wrong in another
- Debug logs show correct UTC time but wrong local time

## Root Cause
`datetime.now()` returns local system time — which may not match user's timezone. `datetime.utcnow()` returns UTC but has no timezone info (naive datetime). Mixing naive and timezone-aware datetimes causes errors or silent wrong results. The agent may assume the server's timezone equals the user's timezone.

## Fix

### Option 1: Always work in UTC internally, convert for display

```python
from datetime import datetime, timezone
import zoneinfo  # Python 3.9+

# WRONG — naive datetime, assumes local timezone
scheduled_at = datetime.now() + timedelta(hours=2)

# RIGHT — always UTC for storage/scheduling
scheduled_at_utc = datetime.now(timezone.utc) + timedelta(hours=2)

# Convert to user's timezone for display only
user_tz = zoneinfo.ZoneInfo("America/New_York")
scheduled_local = scheduled_at_utc.astimezone(user_tz)
print(f"Scheduled for: {scheduled_local.strftime('%Y-%m-%d %I:%M %p %Z')}")
# "Scheduled for: 2025-04-15 09:00 AM EDT"
```

### Option 2: Include timezone in system prompt

```
System prompt:
"User timezone: America/New_York (UTC-5, or UTC-4 during daylight saving time)
Current time in user's timezone: {current_local_time}
Current time in UTC: {current_utc_time}

When scheduling tasks:
- Always interpret times in the user's timezone unless they specify otherwise
- Store times internally as UTC
- Display times in the user's local timezone
- Be aware of daylight saving time transitions"
```

```python
import zoneinfo
from datetime import datetime

def get_system_prompt_with_time(user_timezone: str = "America/New_York") -> str:
    tz = zoneinfo.ZoneInfo(user_timezone)
    now_local = datetime.now(tz)
    now_utc = datetime.now(datetime.timezone.utc)

    return f"""User timezone: {user_timezone}
Current local time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}
Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}

Interpret all times in user's timezone unless told otherwise."""
```

### Option 3: Parse natural language times with timezone

```python
import dateparser
from datetime import timezone
import zoneinfo

def parse_user_time(time_str: str, user_timezone: str) -> datetime:
    """Parse natural language time expressions with user's timezone"""
    settings = {
        "TIMEZONE": user_timezone,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_FUTURE_DATE": True,
        "TO_TIMEZONE": "UTC",  # Convert result to UTC
    }
    parsed = dateparser.parse(time_str, settings=settings)
    if parsed is None:
        raise ValueError(f"Could not parse time: '{time_str}'")
    return parsed

# Usage
user_tz = "Asia/Seoul"
run_time = parse_user_time("9 AM tomorrow", user_tz)
print(f"Will run at: {run_time.isoformat()}Z (UTC)")
# "Will run at: 2025-04-16T00:00:00+00:00Z (UTC)"
# (9 AM Seoul = midnight UTC)
```

### Option 4: APScheduler with timezone support

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import zoneinfo

scheduler = AsyncIOScheduler()

def schedule_task_for_user(task_fn, hour: int, minute: int, user_timezone: str):
    """Schedule task at user's local time"""
    tz = zoneinfo.ZoneInfo(user_timezone)
    scheduler.add_job(
        task_fn,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id=f"task_{user_timezone}_{hour}_{minute}"
    )
    print(f"Scheduled at {hour:02d}:{minute:02d} {user_timezone}")

scheduler.start()

# Schedule daily 9 AM report in user's timezone
schedule_task_for_user(send_daily_report, hour=9, minute=0, user_timezone="Europe/London")
```

### Option 5: Cron with explicit timezone

```python
import subprocess
from datetime import datetime
import zoneinfo

def create_cron_with_timezone(cron_expr: str, command: str, user_timezone: str) -> str:
    """Generate cron entry that respects user timezone"""
    # Convert user time to server timezone
    # Assumes server is UTC — convert cron hour from user tz to UTC
    server_tz = zoneinfo.ZoneInfo("UTC")
    user_tz = zoneinfo.ZoneInfo(user_timezone)

    # Example: convert 9 AM user time to UTC hour
    user_9am = datetime.now(user_tz).replace(hour=9, minute=0, second=0, microsecond=0)
    utc_hour = user_9am.astimezone(server_tz).hour

    utc_cron = f"0 {utc_hour} * * * {command}"
    return utc_cron

# Systemd timer approach (more reliable than cron for DST)
SYSTEMD_TIMER = """
[Timer]
OnCalendar=*-*-* 09:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
"""
```

### Option 6: Validate timezone input from users

```python
import zoneinfo

COMMON_TIMEZONE_ALIASES = {
    "EST": "America/New_York",
    "PST": "America/Los_Angeles",
    "CST": "America/Chicago",
    "MST": "America/Denver",
    "KST": "Asia/Seoul",
    "JST": "Asia/Tokyo",
    "CET": "Europe/Berlin",
    "GMT": "Etc/GMT",
}

def normalize_timezone(user_input: str) -> str:
    """Convert timezone aliases to IANA timezone names"""
    # Try alias lookup first
    tz_name = COMMON_TIMEZONE_ALIASES.get(user_input.upper(), user_input)

    # Validate it's a real IANA timezone
    try:
        zoneinfo.ZoneInfo(tz_name)
        return tz_name
    except zoneinfo.ZoneInfoNotFoundError:
        raise ValueError(
            f"Unknown timezone '{user_input}'. "
            f"Use IANA format like 'America/New_York' or 'Asia/Seoul'. "
            f"Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )
```

## Common Timezone Mistakes

| Mistake | Fix |
|---|---|
| `datetime.now()` with no tz | `datetime.now(timezone.utc)` |
| Storing local time in DB | Store UTC, convert on display |
| Hardcoding "EST" (ignores DST) | Use "America/New_York" |
| Cron job with no CRON_TZ | Add `CRON_TZ=America/New_York` |
| `timedelta(hours=5)` offset | Use `zoneinfo.ZoneInfo` |
| Assuming server tz = user tz | Always ask or infer user timezone |

## Expected Token Savings
Debugging wrong-time bugs: ~5,000 tokens
Timezone-aware scheduling from the start: prevents all issues

## Environment
- Any agent doing scheduling, reminders, or time-based automation
- Source: direct experience; timezone bugs are universally common
