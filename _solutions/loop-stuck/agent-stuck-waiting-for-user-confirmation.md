---
layout: solution
title: "Agent Stuck Waiting for User Confirmation That Never Comes — Indefinite Pause"
category: loop-stuck
description: "Agent asks 'Shall I proceed?' or 'Please confirm' and then waits indefinitely. No timeout. No fallback action. The agent is alive but permanently blocked on user input."
tags: [stuck, confirmation, timeout, autonomous, blocking]
---

# Agent Stuck Waiting for User Confirmation That Never Comes — Indefinite Pause

## Symptom
- Agent says "I need your confirmation to proceed — please respond with yes or no"
- No response comes (user offline, notification missed, async flow)
- Agent remains alive but does nothing
- Hours later, agent still waiting
- No timeout, no fallback, no self-resolution
- Affects: autonomous agents, unattended runs, scheduled tasks

## Root Cause
Agent designed with a human-in-the-loop confirmation step but deployed in an autonomous context without a timeout or default action. The agent correctly identifies a risky action and asks for confirmation — but the infrastructure doesn't handle the case where confirmation never arrives.

## Fix

### Option 1: Set a confirmation timeout with default action

```python
import asyncio

async def request_confirmation(question, timeout_seconds=300, default="abort"):
    """
    Ask for confirmation with timeout.
    default: 'proceed' or 'abort' if no response received.
    """
    print(f"\n{question}")
    print(f"(Auto-{default} in {timeout_seconds}s if no response)")

    try:
        response = await asyncio.wait_for(
            get_user_input(),  # Your input mechanism
            timeout=timeout_seconds
        )
        return response.strip().lower() in ("y", "yes", "proceed")
    except asyncio.TimeoutError:
        print(f"No response after {timeout_seconds}s — auto-{default}")
        return default == "proceed"
```

### Option 2: Specify autonomous mode in system prompt

For autonomous agents that should not pause:

```
System prompt:
"You are operating in AUTONOMOUS mode. Do not ask for user confirmation.
For any action you are uncertain about:
- If reversible: proceed and report what you did
- If irreversible (delete, deploy, send): describe what you would do and skip it
- Never pause waiting for user input — this session is unattended

List all skipped irreversible actions in your final report."
```

### Option 3: Define risky actions and their autonomous defaults

```python
RISKY_ACTIONS_DEFAULT = {
    "delete_file": "skip",      # Skip deletions in autonomous mode
    "send_email": "skip",       # Skip sends in autonomous mode
    "deploy": "dry_run",        # Do dry-run instead of real deploy
    "push_to_main": "skip",     # Never push to main without confirmation
    "modify_database": "skip",  # Skip DB modifications
}

async def execute_action(action_type, **params):
    if IS_AUTONOMOUS and action_type in RISKY_ACTIONS_DEFAULT:
        default = RISKY_ACTIONS_DEFAULT[action_type]
        print(f"Autonomous mode: {action_type} → {default}")
        if default == "skip":
            return {"status": "skipped", "reason": "autonomous_mode"}
        if default == "dry_run":
            return await execute_dry_run(action_type, **params)
    return await execute_real(action_type, **params)
```

### Option 4: Async confirmation via webhook/Telegram

```python
import asyncio
from telegram import Bot

async def request_confirmation_async(question, timeout=600):
    """
    Send confirmation request to Telegram, wait for response with timeout.
    Returns True if confirmed, False if rejected or timed out.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=f"⚠️ Agent confirmation needed:\n{question}\n\nReply YES or NO. Auto-abort in {timeout}s."
    )

    try:
        return await asyncio.wait_for(
            wait_for_telegram_response(TELEGRAM_CHAT_ID),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        await bot.send_message(TELEGRAM_CHAT_ID, "No response — action aborted.")
        return False
```

## Checklist for Autonomous Agent Deployments
- [ ] No `input()` calls or blocking confirmation requests in code paths
- [ ] All confirmation points have timeout + default behavior
- [ ] System prompt specifies autonomous mode behavior explicitly
- [ ] Irreversible actions either skipped or use dry-run in autonomous mode
- [ ] Alert sent if agent is blocked for >N minutes

## Expected Token Savings
Agent stuck for 8 hours: 0 tokens wasted (it's not running), but task is lost
Proper timeout + fallback: task completes or fails cleanly

## Environment
- Autonomous agents, scheduled tasks, unattended runs
- Any agent with human-in-the-loop confirmation steps
- Source: direct experience with production autonomous agent deployments
