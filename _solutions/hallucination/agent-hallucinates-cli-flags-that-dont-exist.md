---
layout: solution
title: "Agent hallucinates CLI flags that don't exist"
category: hallucination
description: "Agent generates shell commands with plausible-looking but nonexistent flags (--recursive on a tool that doesn't support it, --output-format=json for a CLI that only supports --format). Commands fail silently or with cryptic errors."
tags: [hallucination, cli, shell, validation, grounding, tool-use]
---

## Symptom

The agent generates a shell command like `mytool --verbose --output-format=json --recursive` that looks correct, but running it produces "unknown option: --recursive" or, worse, silently ignores unrecognized flags and returns wrong output. The user pastes the command, it fails, and they need another turn to debug what the agent invented.

## Root Cause

CLI flag names follow common conventions (`--verbose`, `--output`, `--format`, `--recursive`) that the model has seen thousands of times across different tools. It generalizes these patterns to tools it has less training data for, producing plausible-but-wrong flag combinations. Without a ground-truth manifest of what the actual CLI accepts, the model cannot distinguish valid from hallucinated flags.

## Fix

Inject the real CLI help output or a flag manifest into the system prompt, or validate generated commands against a known-good flag list before returning them to the user.

---

### Option 1 — Inject `--help` output into system prompt

```python
import anthropic
import subprocess
from functools import lru_cache

client = anthropic.Anthropic(api_key="sk-live-...")


@lru_cache(maxsize=32)
def get_cli_help(cli_name: str) -> str:
    """
    Run `cli_name --help` and return the output.
    Cached so it runs once per CLI per process.
    """
    for flag in ("--help", "-h", "help"):
        try:
            result = subprocess.run(
                [cli_name, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout or result.stderr
            if output.strip():
                return output[:3000]   # cap to avoid overwhelming the context
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return f"CLI '{cli_name}' not found or help unavailable."


def run_agent_with_cli_context(
    user_message: str,
    cli_names: list[str],
) -> str:
    help_sections = []
    for cli in cli_names:
        help_text = get_cli_help(cli)
        help_sections.append(f"## `{cli}` flags (from --help):\n```\n{help_text}\n```")

    system = (
        "You generate shell commands. Use ONLY the flags shown in the help text below. "
        "Do not invent flags. If a capability isn't listed, say it's not supported.\n\n"
        + "\n\n".join(help_sections)
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Injects 200–3000 tokens of help text; prevents one or more follow-up debugging turns (~500–2000 tokens each) caused by nonexistent flags.
**Environment:** Agents that generate commands for a known, fixed set of CLIs; `--help` output is cached after the first call.

---

### Option 2 — Static flag manifest injected as structured context

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Authoritative flag manifests for the CLIs this agent works with
CLI_MANIFESTS: dict[str, dict] = {
    "aws": {
        "s3 cp": ["--recursive", "--acl", "--sse", "--storage-class", "--exclude", "--include"],
        "s3 ls": ["--recursive", "--human-readable", "--summarize", "--page-size"],
        "ec2 describe-instances": ["--instance-ids", "--filters", "--query", "--output", "--region"],
    },
    "docker": {
        "run": ["-d", "-it", "--rm", "--name", "-p", "-v", "-e", "--network", "--entrypoint"],
        "build": ["-t", "-f", "--no-cache", "--build-arg", "--target", "--platform"],
        "ps": ["-a", "-q", "--filter", "--format", "--no-trunc"],
    },
    "git": {
        "log": ["--oneline", "--graph", "--all", "--since", "--until", "-n", "--author", "--grep"],
        "diff": ["--stat", "--name-only", "--cached", "--word-diff", "--color"],
        "push": ["-u", "--force", "--force-with-lease", "--tags", "--dry-run"],
    },
}


def manifest_to_prompt(cli: str) -> str:
    if cli not in CLI_MANIFESTS:
        return f"No manifest available for '{cli}'."
    lines = [f"## {cli} supported flags:"]
    for subcommand, flags in CLI_MANIFESTS[cli].items():
        lines.append(f"  {cli} {subcommand}: {', '.join(flags)}")
    return "\n".join(lines)


def run_agent(user_message: str, relevant_clis: list[str] | None = None) -> str:
    clis = relevant_clis or list(CLI_MANIFESTS.keys())
    manifest_context = "\n\n".join(manifest_to_prompt(c) for c in clis)

    system = (
        "You generate shell commands. Only use flags explicitly listed in the manifests below. "
        "If the user needs functionality not covered by these flags, say so explicitly.\n\n"
        f"{manifest_context}"
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Manifest adds ~200–500 tokens but is compact and precise; far less than injecting full --help output.
**Environment:** Agents working with a controlled set of well-known CLIs; the manifest can be maintained in a config file alongside the agent code.

---

### Option 3 — Post-generation flag validator with re-prompt

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Known valid flags per CLI command (extend as needed)
KNOWN_FLAGS: dict[str, set[str]] = {
    "git": {"--oneline", "--graph", "--all", "--cached", "--stat", "--name-only",
             "--force", "--force-with-lease", "--tags", "--dry-run", "--verbose",
             "--quiet", "--no-pager", "-u", "-n", "-p"},
    "docker": {"--rm", "--detach", "-d", "--interactive", "-i", "--tty", "-t",
                "--name", "--publish", "-p", "--volume", "-v", "--env", "-e",
                "--network", "--entrypoint", "--no-cache", "--build-arg"},
    "aws": {"--recursive", "--output", "--query", "--region", "--profile",
             "--dry-run", "--filters", "--instance-ids", "--acl", "--sse"},
}

# Pattern: matches --flag or --flag=value or -f
FLAG_PATTERN = re.compile(r"(?:^|\s)(--[\w-]+(?:=\S*)?|-[a-zA-Z])", re.MULTILINE)


def extract_commands(text: str) -> list[tuple[str, str]]:
    """Extract (cli_name, full_command) pairs from code blocks."""
    results = []
    for block in re.findall(r"```(?:bash|sh|shell)?\s*(.*?)```", text, re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first_word = line.split()[0] if line.split() else ""
            if first_word in KNOWN_FLAGS:
                results.append((first_word, line))
    return results


def validate_flags(cli: str, command: str) -> list[str]:
    """Return list of unrecognized flags found in the command."""
    if cli not in KNOWN_FLAGS:
        return []
    flags_in_cmd = {m.group(1) for m in FLAG_PATTERN.finditer(command)}
    long_flags = {f.split("=")[0] for f in flags_in_cmd if f.startswith("--")}
    return [f for f in long_flags if f not in KNOWN_FLAGS[cli]]


def run_agent_with_validation(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=messages,
    )
    output = response.content[0].text

    # Check for hallucinated flags
    bad_flags: list[str] = []
    for cli, cmd in extract_commands(output):
        bad_flags.extend(validate_flags(cli, cmd))

    if bad_flags:
        print(f"Hallucinated flags detected: {bad_flags} — requesting correction")
        messages.append({"role": "assistant", "content": output})
        messages.append({
            "role": "user",
            "content": (
                f"Your command used these flags that don't exist for this tool: {bad_flags}. "
                f"Please rewrite the command using only valid flags. "
                f"Valid flags for {list(KNOWN_FLAGS.keys())} are: "
                + ", ".join(f"{k}: {sorted(v)}" for k, v in KNOWN_FLAGS.items())
            ),
        })
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text

    return output
```

**Expected Token Savings:** Correction turn costs ~400 tokens; prevents user from running a broken command and returning for a second round of debugging.
**Environment:** Agents that generate commands for CLIs with well-known flag sets; the validator catches the most common hallucinations without needing full --help injection.

---

### Option 4 — Tool-based command validation: agent calls a `validate_command` tool

```python
import anthropic
import shlex

client = anthropic.Anthropic(api_key="sk-live-...")

# Validation database
VALID_COMMANDS: dict[str, dict] = {
    "curl": {
        "flags": {"-X", "-H", "-d", "-o", "-L", "-s", "-S", "-v", "-k",
                  "--request", "--header", "--data", "--output", "--location",
                  "--silent", "--show-error", "--verbose", "--insecure",
                  "--max-time", "--retry", "--compressed", "--include"},
        "description": "HTTP client",
    },
    "jq": {
        "flags": {"-r", "-c", "-n", "-e", "-s", "-R", "-f",
                  "--raw-output", "--compact-output", "--null-input",
                  "--exit-status", "--slurp", "--raw-input", "--from-file"},
        "description": "JSON processor",
    },
}

VALIDATE_TOOL = {
    "name": "validate_command",
    "description": (
        "Validate a shell command before returning it to the user. "
        "Returns 'ok' if all flags are valid, or a list of invalid flags. "
        "Call this for every generated shell command."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The full shell command to validate."},
            "cli_name": {"type": "string", "description": "The CLI tool name (e.g. 'curl', 'jq')."},
        },
        "required": ["command", "cli_name"],
    },
}

SYSTEM = (
    "You generate shell commands. After generating a command, always call "
    "validate_command to check it before including it in your response. "
    "If validation fails, fix the command and validate again."
)


def handle_validate_command(command: str, cli_name: str) -> str:
    db = VALID_COMMANDS.get(cli_name)
    if db is None:
        return f"ok (no manifest for '{cli_name}' — unable to validate)"

    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return f"parse error: {e}"

    flags_used = {t for t in tokens if t.startswith("-")}
    long_flags = {t.split("=")[0] for t in flags_used if t.startswith("--")}
    short_flags = {t for t in flags_used if t.startswith("-") and not t.startswith("--")}

    invalid = [f for f in long_flags | short_flags if f not in db["flags"]]
    if invalid:
        return f"invalid flags: {sorted(invalid)}. Valid flags: {sorted(db['flags'])}"
    return "ok"


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(6):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=[VALIDATE_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "validate_command":
                    result = handle_validate_command(**block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** The validation tool adds one round-trip; the model self-corrects before the command reaches the user — no user-visible failure.
**Environment:** Interactive agents where users run generated commands immediately; the tool makes the validation loop explicit and auditable.

---

### Option 5 — Safe command runner: dry-run before committing

```python
import anthropic
import subprocess
import re
import shlex

client = anthropic.Anthropic(api_key="sk-live-...")

# CLIs that support a safe dry-run mode
DRY_RUN_FLAGS: dict[str, list[str]] = {
    "rsync": ["--dry-run"],
    "aws": ["--dry-run"],
    "git": ["--dry-run"],
    "make": ["-n", "--dry-run"],
    "ansible-playbook": ["--check"],
    "terraform": ["plan"],
}


def extract_first_command(text: str) -> str | None:
    """Extract the first shell command from a code block."""
    match = re.search(r"```(?:bash|sh|shell)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        lines = [l.strip() for l in match.group(1).splitlines() if l.strip() and not l.startswith("#")]
        return lines[0] if lines else None
    return None


def dry_run_validate(command: str) -> tuple[bool, str]:
    """
    Attempt a dry-run of the command to catch invalid flags before real execution.
    Returns (success, output_or_error).
    """
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return False, f"Parse error: {e}"

    cli = tokens[0] if tokens else ""
    dry_flags = DRY_RUN_FLAGS.get(cli)
    if not dry_flags:
        return True, "(no dry-run available for this CLI)"

    # Build dry-run command
    dry_cmd = tokens + dry_flags
    try:
        result = subprocess.run(
            dry_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout[:500]
        # Check for "unrecognized option" type errors
        stderr = result.stderr.lower()
        if any(p in stderr for p in ("unrecognized", "unknown option", "invalid option")):
            return False, f"Invalid flag: {result.stderr[:300]}"
        return False, result.stderr[:300]
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text
    command = extract_first_command(output)

    if command:
        ok, result = dry_run_validate(command)
        if not ok:
            print(f"Dry-run failed for `{command}`: {result}")
            # Return the output with a warning appended
            output += f"\n\n⚠️ Validation warning: `{command}` may have invalid flags — {result}"

    return output
```

**Expected Token Savings:** None on tokens; the dry-run runs in a subprocess and catches flag errors before the user does.
**Environment:** Agents with access to a shell where the relevant CLIs are installed; dry-runs are cheap and surface real errors from the actual binary.

---

### Option 6 — Few-shot grounding: anchor with verified command examples

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Curated, verified examples for the CLIs the agent works with
VERIFIED_EXAMPLES = """
## Verified curl examples (all flags confirmed valid):
- List endpoint: curl -s -X GET https://api.example.com/items -H "Authorization: Bearer TOKEN"
- Post JSON: curl -s -X POST https://api.example.com/items -H "Content-Type: application/json" -d '{"name":"test"}'
- Download file: curl -L -o output.zip https://example.com/file.zip
- Follow redirects silently: curl -sL https://example.com/redirect

## Verified git examples:
- Compact log: git log --oneline -n 20
- Show staged changes: git diff --cached --stat
- Push new branch: git push -u origin feature-branch
- Dry-run push: git push --dry-run origin main

## Verified docker examples:
- Run detached: docker run -d --name myapp -p 8080:80 myimage:latest
- Build with tag: docker build -t myimage:v1.0 -f Dockerfile .
- List running: docker ps --format "table {{.Names}}\t{{.Status}}"

IMPORTANT: Only use flags shown in these examples or documented in --help.
Do not generalize to other flags even if they seem plausible.
"""

SYSTEM = (
    "You are a shell command assistant. Generate commands using only verified, documented flags. "
    "Use the examples below as your ground truth.\n\n"
    f"{VERIFIED_EXAMPLES}"
)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Comparison table
# | Option | Ground Truth Source | Catches Bad Flags | Exec Required |
# |--------|--------------------|--------------------|---------------|
# | 1 --help injection | Live CLI output | Yes (via model) | Yes (once) |
# | 2 Static manifest | Config file | Yes (via model) | No |
# | 3 Post-gen validator | Code regex | Yes (code check) | No |
# | 4 Validate tool | Tool manifest | Yes (tool call) | No |
# | 5 Dry-run | Real CLI binary | Yes (runtime) | Yes (per call) |
# | 6 Few-shot examples | Curated examples | Partially | No |
```

**Expected Token Savings:** Few-shot examples add ~400–600 tokens but anchor the model to patterns it has already verified; reduces hallucinated flags by establishing a concrete reference set.
**Environment:** Agents where live CLI execution isn't available (sandboxed environments, code generation tools); the example set is maintained manually but requires no runtime dependencies.
