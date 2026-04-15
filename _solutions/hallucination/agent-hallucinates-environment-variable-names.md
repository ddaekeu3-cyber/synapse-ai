---
layout: solution
title: "Agent hallucinates environment variable names"
category: hallucination
description: "Agent invents environment variable names (DATABASE_HOST, OPENAI_KEY) that don't match the actual names in the deployment (DB_HOST, ANTHROPIC_API_KEY), causing silent misconfiguration and hard-to-debug runtime failures."
tags: [hallucination, environment-variables, configuration, validation, runtime]
---

## Symptom

The agent generates setup instructions, configuration code, or tool calls that reference `os.environ["DATABASE_HOST"]` or `os.environ["OPENAI_KEY"]` — names that don't exist in your deployment. The result is either a `KeyError` at runtime, a silent `None` from `os.getenv()`, or an agent that confidently produces code that silently reads the wrong value (or nothing at all).

## Root Cause

The model generates plausible-sounding environment variable names from training data patterns. It has seen thousands of config files and generalizes: `DATABASE_HOST`, `DB_HOST`, `POSTGRES_HOST`, and `PGHOST` are all reasonable guesses. Without a ground-truth list of actual variable names, it picks whichever looks most idiomatic — often wrong.

## Fix

Inject the authoritative list of available environment variable names into the system prompt or as tool context. Validate any variable name the model produces against that list before using it.

---

### Option 1 — Inject available env var names into the system prompt

```python
import anthropic
import os

client = anthropic.Anthropic(api_key="sk-live-...")

# The actual variable names present in this deployment
KNOWN_ENV_VARS = [
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "ANTHROPIC_API_KEY",
    "REDIS_URL",
    "S3_BUCKET_NAME",
    "AWS_REGION",
    "LOG_LEVEL",
    "APP_ENV",
    "SECRET_KEY",
]


def make_system_prompt() -> str:
    var_list = "\n".join(f"  - {v}" for v in sorted(KNOWN_ENV_VARS))
    return (
        "You are a configuration assistant.\n\n"
        "The following environment variables are available in this deployment. "
        "Only reference these exact names — do not invent or guess others:\n"
        f"{var_list}\n\n"
        "If a required variable is not in this list, say so explicitly instead of inventing a name."
    )


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=make_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Adds ~30–100 tokens depending on the number of variables; prevents multi-turn debugging sessions caused by missing env vars.
**Environment:** Any agent that generates configuration code or setup instructions; requires maintaining the `KNOWN_ENV_VARS` list in sync with your deployment.

---

### Option 2 — Validate model-generated var names against actual environment

```python
import anthropic
import os
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Pattern that matches env var names in code: os.environ["VAR"] or os.getenv("VAR")
ENV_VAR_PATTERN = re.compile(
    r'os\.(?:environ(?:\[[\'"]([\w]+)[\'"]\]|\.get\([\'"](\w+)[\'"])|getenv\([\'"](\w+)[\'"])'
)


def extract_env_var_names(code: str) -> set[str]:
    """Extract all env var names referenced in a code snippet."""
    names: set[str] = set()
    for m in ENV_VAR_PATTERN.finditer(code):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            names.add(name)
    return names


def validate_env_vars(code: str) -> tuple[set[str], set[str]]:
    """
    Returns (found_vars, missing_vars).
    found_vars: names present in both code and os.environ
    missing_vars: names in code but NOT in os.environ
    """
    referenced = extract_env_var_names(code)
    available = set(os.environ.keys())
    found = referenced & available
    missing = referenced - available
    return found, missing


def run_agent_with_validation(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text

    found, missing = validate_env_vars(output)
    if missing:
        print(f"WARNING: Model referenced undefined env vars: {sorted(missing)}")
        print(f"         Available vars used correctly: {sorted(found)}")
        # Optionally: re-prompt with correction
        correction = (
            f"Your code references these environment variables that don't exist in this deployment: "
            f"{sorted(missing)}. "
            f"Please rewrite the code using only variables that exist. "
            f"Available variables include: {sorted(os.environ.keys())[:20]}..."
        )
        response2 = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": output},
                {"role": "user", "content": correction},
            ],
        )
        return response2.content[0].text

    return output
```

**Expected Token Savings:** One correction turn costs ~500 tokens; prevents silent misconfiguration that could require hours of debugging.
**Environment:** Agents that generate Python configuration or deployment code; requires the agent's execution environment to have the real env vars set.

---

### Option 3 — Env var lookup tool that the agent must call

```python
import anthropic
import os

client = anthropic.Anthropic(api_key="sk-live-...")

# Allowlist of vars the agent is permitted to read
PERMITTED_VARS = {
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER",
    "REDIS_URL", "S3_BUCKET_NAME", "AWS_REGION",
    "LOG_LEVEL", "APP_ENV",
}

ENV_TOOL = {
    "name": "get_env_var",
    "description": (
        "Look up the value of a deployment environment variable. "
        "Always use this tool to retrieve env var values — never guess them. "
        "Returns the value if present, or an error message if the variable doesn't exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The exact environment variable name, e.g. 'DB_HOST'.",
            }
        },
        "required": ["name"],
    },
}

SYSTEM = (
    "You are a deployment configuration assistant. "
    "When you need an environment variable value, call get_env_var with the exact name. "
    "Never invent or guess variable names — only use names confirmed by get_env_var."
)


def handle_get_env_var(name: str) -> str:
    if name not in PERMITTED_VARS:
        available = sorted(PERMITTED_VARS)
        return f"ERROR: '{name}' is not in the permitted variable list. Available: {available}"
    value = os.environ.get(name)
    if value is None:
        return f"ERROR: '{name}' is permitted but not set in the current environment."
    # Redact sensitive values in logs (show only that it exists)
    is_secret = any(s in name.upper() for s in ("PASSWORD", "SECRET", "KEY", "TOKEN"))
    display = "[REDACTED]" if is_secret else value
    return f"OK: {name}={display}"


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=[ENV_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "get_env_var":
                    result = handle_get_env_var(block.input["name"])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** None — adds tool round-trips; prevents incorrect values from propagating into generated configs.
**Environment:** Agents that need to read and reason about live config values, not just generate code; the tool acts as an auditable lookup with access control.

---

### Option 4 — Fuzzy match: correct near-miss variable names before use

```python
import anthropic
import os
import difflib

client = anthropic.Anthropic(api_key="sk-live-...")

CANONICAL_VARS = {
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
    "ANTHROPIC_API_KEY", "REDIS_URL", "S3_BUCKET_NAME",
    "AWS_REGION", "LOG_LEVEL", "APP_ENV", "SECRET_KEY",
}


def correct_var_name(name: str, cutoff: float = 0.7) -> str | None:
    """
    If `name` is not in CANONICAL_VARS, find the closest match.
    Returns the corrected name, or None if no close match found.
    """
    if name in CANONICAL_VARS:
        return name
    matches = difflib.get_close_matches(name, CANONICAL_VARS, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def safe_get_env(name: str) -> str | None:
    """Get env var, auto-correcting near-miss names."""
    canonical = correct_var_name(name)
    if canonical is None:
        raise KeyError(
            f"Environment variable '{name}' not found and no close match in canonical list. "
            f"Known vars: {sorted(CANONICAL_VARS)}"
        )
    if canonical != name:
        print(f"Auto-corrected env var: '{name}' → '{canonical}'")
    return os.environ.get(canonical)


def run_agent(user_message: str) -> str:
    # Inject canonical var names so the model has the right anchors
    var_list = ", ".join(sorted(CANONICAL_VARS))
    system = (
        f"You are a configuration assistant. "
        f"Available environment variables: {var_list}. "
        f"Use only these exact names."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    # Demo: show correction in action
    for hallucinated_name in ["DATABASE_HOST", "OPENAI_KEY", "REDIS_CONNECTION_URL", "APP_SECRET"]:
        corrected = correct_var_name(hallucinated_name)
        print(f"  '{hallucinated_name}' → '{corrected}'")

    return response.content[0].text
```

**Expected Token Savings:** None — the fuzzy matcher runs client-side; it catches and corrects the most common hallucination patterns (plural/singular, prefix differences).
**Environment:** Legacy codebases where env var naming is inconsistent and the model frequently guesses reasonable-but-wrong alternatives.

---

### Option 5 — Schema-driven env var declaration with Pydantic

```python
import anthropic
import os
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class DeploymentConfig(BaseSettings):
    """
    Authoritative declaration of all environment variables for this deployment.
    Pydantic reads these from os.environ at startup and fails fast on missing required vars.
    """
    db_host: str = Field(alias="DB_HOST")
    db_port: int = Field(alias="DB_PORT", default=5432)
    db_name: str = Field(alias="DB_NAME")
    db_user: str = Field(alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")
    redis_url: str = Field(alias="REDIS_URL", default="redis://localhost:6379")
    log_level: str = Field(alias="LOG_LEVEL", default="INFO")
    app_env: str = Field(alias="APP_ENV", default="development")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got '{v}'")
        return v.upper()

    model_config = {"populate_by_name": True}


# config = DeploymentConfig()  # fails fast if required vars are missing


client = anthropic.Anthropic(api_key="sk-live-...")


def make_system_prompt_from_schema() -> str:
    """Generate the system prompt from the Pydantic model's field aliases."""
    fields = DeploymentConfig.model_fields
    lines = []
    for field_name, field_info in fields.items():
        alias = field_info.alias or field_name.upper()
        default = field_info.default
        required = default is None
        lines.append(f"  {alias} ({'required' if required else f'optional, default={default!r}'})")
    var_block = "\n".join(lines)
    return (
        "You are a deployment assistant. The following environment variables are defined:\n"
        f"{var_block}\n\n"
        "Only reference these exact names in any configuration code you produce."
    )


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=make_system_prompt_from_schema(),
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None — Pydantic adds startup validation that surfaces missing vars before any LLM calls are made; the schema-derived system prompt keeps the model aligned with the actual config.
**Environment:** Python services already using `pydantic-settings`; the schema is the single source of truth for both runtime validation and LLM context.

---

### Option 6 — `.env` file scanner: inject only vars from actual env file

```python
import anthropic
import os
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")


def load_env_file(path: str = ".env") -> dict[str, str]:
    """
    Parse a .env file and return name→value pairs.
    Handles comments, blank lines, quoted values, and export prefix.
    """
    env_path = Path(path)
    if not env_path.exists():
        return {}

    result: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        result[name] = value
    return result


def make_system_prompt_from_env_file(env_path: str = ".env") -> str:
    env_vars = load_env_file(env_path)
    if not env_vars:
        return "You are a configuration assistant. No .env file found."

    # Show names but redact sensitive values
    sensitive_keywords = {"PASSWORD", "SECRET", "KEY", "TOKEN", "CREDENTIAL"}
    lines = []
    for name in sorted(env_vars):
        is_sensitive = any(kw in name.upper() for kw in sensitive_keywords)
        display_value = "[REDACTED]" if is_sensitive else env_vars[name]
        lines.append(f"  {name}={display_value}")

    var_block = "\n".join(lines)
    return (
        "You are a deployment configuration assistant.\n\n"
        "The following environment variables are defined in the .env file:\n"
        f"{var_block}\n\n"
        "Only use these exact variable names. "
        "Do not invent names like DATABASE_HOST when the actual name is DB_HOST."
    )


def run_agent(user_message: str, env_path: str = ".env") -> str:
    system = make_system_prompt_from_env_file(env_path)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Comparison table
# | Option | Ground-Truth Source | Auto-Corrects | Validates at Runtime |
# |--------|--------------------|--------------|--------------------|
# | 1 System prompt injection | Static list | No | No |
# | 2 Post-generation validator | os.environ | Yes (re-prompt) | No |
# | 3 Lookup tool | PERMITTED_VARS set | No | Yes (via tool) |
# | 4 Fuzzy matcher | CANONICAL_VARS set | Yes (difflib) | No |
# | 5 Pydantic schema | Model field aliases | No | Yes (startup) |
# | 6 .env file scanner | .env file | No | No |
```

**Expected Token Savings:** Adds ~50–150 tokens for the var list; prevents one or more follow-up debugging turns that each cost 500–2000 tokens.
**Environment:** Local development or CI pipelines where a `.env` file is the canonical config source; automatically stays in sync as vars are added or renamed.
