---
layout: solution
title: "Agent Blocks Waiting for User Input in Automated Pipeline"
category: loop-stuck
description: "Agent is designed for interactive use and calls input() or waits for user confirmation mid-task. When run in a CI pipeline or automated job, the process hangs forever waiting for input that never comes."
tags: [loop-stuck, input, blocking, automation, ci, pipeline, non-interactive, stdin]
---

# Agent Blocks Waiting for User Input in Automated Pipeline

## Symptom
- CI/CD job hangs indefinitely — no output, no timeout, no completion
- Agent process shows 0% CPU but never exits — waiting on stdin
- Pipeline times out after 1 hour: "Job exceeded maximum execution time"
- `input("Continue? [y/n]: ")` hangs in automated environment with no TTY
- Agent asks for clarification mid-task — automated caller cannot respond
- `getpass.getpass()` blocks without a terminal

## Root Cause
Interactive `input()` calls block until data arrives on stdin. In automated environments (CI runners, Docker without `-it`, cron jobs, systemd services), stdin is either closed, `/dev/null`, or a pipe with no writer. The process waits forever. Even well-intentioned confirmation prompts ("Are you sure you want to delete?") become deadlocks in automation.

## Fix

### Option 1: Detect non-interactive mode and skip prompts

```python
import sys
import os

def is_interactive() -> bool:
    """
    Check if the process is running interactively (has a real terminal).
    Returns False in CI, Docker without -it, cron, pipes, etc.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()

def safe_confirm(prompt: str, default: bool = True, auto_confirm: bool = False) -> bool:
    """
    Ask for confirmation in interactive mode.
    In non-interactive mode, return the default without blocking.
    """
    if auto_confirm or not is_interactive():
        default_str = "yes" if default else "no"
        print(f"{prompt} [auto-{default_str}]")
        return default

    response = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")

def safe_input(prompt: str, default: str = "", auto_value: str = None) -> str:
    """
    Read user input in interactive mode.
    Returns default/auto_value in non-interactive mode.
    """
    if auto_value is not None or not is_interactive():
        value = auto_value or default
        print(f"{prompt}{value} [automated]")
        return value

    return input(prompt) or default

# Usage:
if safe_confirm("Delete 500 records?", default=False):
    delete_records()

# In CI: prints "Delete 500 records? [auto-no]" and skips deletion
# Interactively: asks user and waits for y/n
```

### Option 2: Timeout-based input with automatic fallback

```python
import signal
import sys

class InputTimeout(Exception):
    pass

def timed_input(prompt: str, timeout: float = 30.0, default: str = "") -> str:
    """
    Read input with a timeout. Returns default if no response within timeout.
    """
    if not sys.stdin.isatty():
        # Non-interactive: return default immediately
        print(f"{prompt}[no-tty, using default: '{default}']")
        return default

    def _timeout_handler(signum, frame):
        raise InputTimeout()

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(int(timeout))

    try:
        print(f"{prompt}(auto-select '{default}' in {timeout:.0f}s) ", end="", flush=True)
        response = input()
        return response or default
    except InputTimeout:
        print(f"\nTimeout — using default: '{default}'")
        return default
    except EOFError:
        # stdin was closed (piped input exhausted)
        print(f"\nEOF — using default: '{default}'")
        return default
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

# Works in all environments:
answer = timed_input("Choose action [skip/retry/abort]: ", timeout=30, default="skip")
```

### Option 3: Use environment variables for automation decisions

```python
import os

class AutomationConfig:
    """
    Read decisions from environment variables for automated runs.
    Fall back to interactive prompts when running manually.
    """

    # Set these env vars in CI to control agent behavior:
    # AGENT_AUTO_CONFIRM=true — confirm all destructive actions
    # AGENT_AUTO_SKIP=true — skip all optional steps
    # AGENT_DRY_RUN=true — simulate without making changes
    # AGENT_MAX_RETRIES=3 — retry count without asking

    def __init__(self):
        self.auto_confirm = os.environ.get("AGENT_AUTO_CONFIRM", "").lower() == "true"
        self.auto_skip = os.environ.get("AGENT_AUTO_SKIP", "").lower() == "true"
        self.dry_run = os.environ.get("AGENT_DRY_RUN", "").lower() == "true"
        self.max_retries = int(os.environ.get("AGENT_MAX_RETRIES", "3"))
        self.non_interactive = not sys.stdin.isatty()

    def confirm_action(self, action: str, is_destructive: bool = False) -> bool:
        if self.dry_run:
            print(f"[DRY RUN] Would execute: {action}")
            return False

        if self.auto_confirm:
            print(f"[AUTO] Confirmed: {action}")
            return True

        if self.auto_skip and not is_destructive:
            print(f"[AUTO SKIP] Skipping: {action}")
            return False

        if self.non_interactive:
            # Default: skip non-destructive, refuse destructive
            decision = not is_destructive
            print(f"[NON-INTERACTIVE] {'Proceeding' if decision else 'Skipping'}: {action}")
            return decision

        return input(f"Execute '{action}'? [y/N]: ").strip().lower() == "y"

cfg = AutomationConfig()

# In CI with AGENT_AUTO_CONFIRM=true: all actions proceed automatically
# Locally without env vars: interactive prompts
if cfg.confirm_action("migrate database schema", is_destructive=True):
    run_migration()
```

### Option 4: Fail-fast instead of blocking on missing input

```python
import sys

class MissingRequiredInput(Exception):
    """Raised when required input is unavailable in non-interactive mode"""
    pass

def require_input(
    prompt: str,
    env_var: str = None,
    required: bool = True,
    default: str = None
) -> str:
    """
    Get required input from env var, stdin, or fail fast.
    In automated mode: env var → default → fail.
    """
    # Check env var first
    if env_var:
        value = os.environ.get(env_var, "")
        if value:
            return value

    # Non-interactive: can't prompt
    if not sys.stdin.isatty():
        if default is not None:
            print(f"Non-interactive mode: using default for '{prompt}': '{default}'")
            return default
        if required:
            raise MissingRequiredInput(
                f"Required input '{prompt}' not provided in non-interactive mode.\n"
                f"Set environment variable '{env_var}' before running." if env_var
                else f"Required input '{prompt}' not provided and no env var configured."
            )
        return ""

    return input(prompt)

# Fail fast with a clear message instead of hanging:
try:
    api_key = require_input(
        "Enter API key: ",
        env_var="MY_SERVICE_API_KEY",
        required=True
    )
except MissingRequiredInput as e:
    print(f"ERROR: {e}")
    sys.exit(1)
```

### Option 5: Pre-flight check for automation requirements

```python
def check_automation_prerequisites() -> None:
    """
    Before running in automated mode, verify all required inputs are available.
    Fail at startup with clear message rather than blocking mid-execution.
    """
    if sys.stdin.isatty():
        return  # Interactive mode — prompts will work

    required_env_vars = [
        ("AGENT_TASK", "The task to execute"),
        ("AGENT_OUTPUT_DIR", "Where to write output"),
        ("MY_API_KEY", "API key for the service"),
    ]

    optional_env_vars = [
        ("AGENT_DRY_RUN", "Set to 'true' to simulate"),
        ("AGENT_AUTO_CONFIRM", "Set to 'true' to skip confirmations"),
    ]

    missing = []
    for var, description in required_env_vars:
        if not os.environ.get(var):
            missing.append(f"  {var}: {description}")

    if missing:
        print("ERROR: Running in non-interactive mode but required env vars are missing:")
        print("\n".join(missing))
        print("\nSet these before running in automation:")
        for var, desc in required_env_vars:
            print(f"  export {var}='...'  # {desc}")
        sys.exit(1)

    print("Automation prerequisites satisfied. Running non-interactively.")
    for var, _ in optional_env_vars:
        val = os.environ.get(var, "not set")
        print(f"  {var}={val}")

# Call at startup:
check_automation_prerequisites()
```

### Option 6: System prompt for non-interactive behavior

```
System prompt:
"Non-interactive operation rules:

This agent runs in automated mode. There is no human available to answer questions.

Rules:
1. NEVER ask the user a question that requires a response before proceeding.
   If you need information, check the provided context or use your best judgment.

2. If you reach a decision point requiring a choice:
   a. Pick the safer/more conservative option automatically
   b. Explain your choice in the output
   c. Continue without waiting

3. If you need confirmation before a destructive action:
   a. Log the action as 'PENDING — requires human review'
   b. Continue with non-destructive steps
   c. Summarize pending actions at the end

4. If information is genuinely missing and you cannot proceed:
   a. State clearly what is missing
   b. State what you completed so far
   c. Exit gracefully — do NOT block or wait"
```

## Interactive vs Automated Mode Decision Matrix

| Situation | Interactive | Automated |
|---|---|---|
| Confirmation needed | Prompt user | Check env var or use safe default |
| Missing optional input | Prompt with default | Use default silently |
| Missing required input | Prompt (required) | Fail fast with clear message |
| Destructive action | Confirm first | Require explicit env var flag |
| Ambiguous input | Ask for clarification | Log ambiguity, use conservative interpretation |

## Expected Token Savings
CI job hanging for 1 hour until timeout: ~0 tokens but hours of blocked pipeline
Non-interactive detection + auto-defaults: job completes in seconds

## Environment
- Any agent intended for both interactive use and automation; critical for CI/CD pipeline integration
- Source: direct experience; stdin blocking is the most common failure mode when moving agents from local dev to CI
