---
layout: solution
title: "Agent hallucinates environment variable names in generated code"
category: hallucination
description: "Agent generates code referencing env vars like OPENAI_SECRET_TOKEN or DATABASE_HOST_URL that don't exist in the runtime environment, causing silent failures or KeyError crashes at startup."
tags: [hallucination, environment-variables, configuration, code-generation, validation]
---

## Symptom

Generated setup scripts or agent configuration code references env vars that don't match the actual runtime environment:

```python
# Generated code — none of these vars exist
api_key = os.environ["OPENAI_SECRET_TOKEN"]      # real: OPENAI_API_KEY
db_url  = os.environ["DATABASE_HOST_URL"]        # real: DATABASE_URL
timeout = os.environ["REQUEST_TIMEOUT_SECONDS"]  # real: TIMEOUT_SEC
```

The service starts, passes CI (which mocks env), and crashes in production with `KeyError` or silently uses a wrong default.

## Root Cause

The model has seen thousands of env var conventions across training data and pattern-matches to plausible-sounding names rather than the actual variable names defined in the project. Without grounding, it invents names that are semantically reasonable but factually wrong. The error is invisible until runtime because Python's `os.environ` raises `KeyError` lazily.

---

## Option 1 — Inject actual env var inventory into the system prompt

**Read `.env.example` or `docker-compose.yml` at request time and append the canonical list to the system prompt.**

```python
import os
import re
import anthropic

client = anthropic.Anthropic()


def load_env_inventory(path: str = ".env.example") -> list[str]:
    """Return variable names declared in .env.example."""
    names: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    match = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
                    if match:
                        names.append(match.group(1))
    except FileNotFoundError:
        pass
    return names


def generate_config_code(task: str) -> str:
    env_vars = load_env_inventory()
    env_block = "\n".join(f"  - {v}" for v in env_vars)

    system = f"""You are a Python code generator.
The ONLY environment variables available in this project are:
{env_block}

Use EXACTLY these names — do not invent or guess alternatives.
"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text


code = generate_config_code(
    "Write a Python snippet that reads the API key and database URL from environment variables."
)
print(code)
```

**`.env.example` used as source of truth — model cannot hallucinate names it doesn't know.**

**Expected Token Savings:** Eliminates debug cycles where hallucinated var names are discovered only at deploy time — prevents the 2–5 follow-up LLM calls needed to diagnose and fix each wrong name.

**Environment:** Projects with `.env.example`; works with `docker-compose.yml` or `terraform.tfvars` as alternative sources.

---

## Option 2 — Post-generation validator that diffs against known vars

**After generation, extract every `os.environ[...]` reference and verify it against the real env var set.**

```python
import ast
import os
import re
import anthropic

client = anthropic.Anthropic()

# Load canonical var names from .env.example + current environment
_KNOWN_VARS: set[str] = set(os.environ.keys())
try:
    with open(".env.example") as f:
        for line in f:
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
            if m:
                _KNOWN_VARS.add(m.group(1))
except FileNotFoundError:
    pass


def extract_env_refs(code: str) -> list[str]:
    """Return all os.environ['VAR'] and os.getenv('VAR') references."""
    refs: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return refs

    for node in ast.walk(tree):
        # os.environ["KEY"] or os.environ.get("KEY")
        if isinstance(node, ast.Subscript):
            if (isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)):
                refs.append(node.slice.value)
        # os.getenv("KEY")
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "getenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                refs.append(node.args[0].value)
    return refs


def generate_and_validate(task: str, max_retries: int = 3) -> str:
    for attempt in range(1, max_retries + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": task}],
        )
        code = response.content[0].text
        refs = extract_env_refs(code)
        unknown = [r for r in refs if r not in _KNOWN_VARS]

        if not unknown:
            print(f"Validated on attempt {attempt}.")
            return code

        # Inject correction into next attempt
        task = (
            f"{task}\n\n"
            f"IMPORTANT: the previous attempt used unknown env vars: {unknown}.\n"
            f"Known vars are: {sorted(_KNOWN_VARS)}.\n"
            f"Fix the code to use only the known vars."
        )
        print(f"Attempt {attempt}: unknown vars {unknown} — retrying …")

    return code  # return best effort after max retries


result = generate_and_validate(
    "Write Python code to connect to the database using environment variables."
)
print(result)
```

**Expected Token Savings:** Catches hallucinations before they reach production, eliminating debugging sessions that typically require 3–8 additional LLM calls to diagnose.

**Environment:** Any code-generation pipeline; requires Python 3.8+ AST module (stdlib).

---

## Option 3 — Structured output with enum-constrained field names

**Define an `EnvConfig` Pydantic model whose field names match the real env vars. Ask the model to populate the struct, not write free-form code.**

```python
import os
from pydantic import BaseModel, Field
import anthropic

client = anthropic.Anthropic()


class EnvConfig(BaseModel):
    """Canonical environment variable mapping for this project."""
    ANTHROPIC_API_KEY: str = Field(description="Anthropic API key")
    DATABASE_URL: str = Field(description="PostgreSQL connection string")
    REDIS_URL: str = Field(description="Redis connection URL")
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    PORT: int = Field(default=8000, description="HTTP server port")


def generate_env_loading_code(task: str) -> str:
    schema_json = EnvConfig.model_json_schema()

    prompt = (
        f"{task}\n\n"
        f"Use ONLY the following environment variable names (from our EnvConfig schema):\n"
        f"{schema_json}\n\n"
        f"Generate Python code that loads these variables using os.environ.get() with the "
        f"exact field names shown in the schema."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# Runtime loading (separate from generation)
def load_config() -> EnvConfig:
    return EnvConfig(
        ANTHROPIC_API_KEY=os.environ["ANTHROPIC_API_KEY"],
        DATABASE_URL=os.environ["DATABASE_URL"],
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO"),
        PORT=int(os.environ.get("PORT", "8000")),
    )


code = generate_env_loading_code(
    "Write the startup config loader for our FastAPI service."
)
print(code)
```

**Expected Token Savings:** Schema-driven generation eliminates all hallucinated var names in a single pass — no retry loop needed.

**Environment:** Projects already using Pydantic for settings management; pairs with `pydantic-settings` for auto-loading.

---

## Option 4 — Few-shot examples using real var names

**Show the model 2–3 correct examples from the same codebase before asking it to generate new code.**

```python
import anthropic

client = anthropic.Anthropic()

# Real snippets from the project (not invented by the model)
FEW_SHOT_EXAMPLES = [
    {
        "task": "Load the API key for Anthropic",
        "code": 'api_key = os.environ["ANTHROPIC_API_KEY"]',
    },
    {
        "task": "Get the database connection string",
        "code": 'db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/dev")',
    },
    {
        "task": "Read the cache TTL in seconds",
        "code": 'cache_ttl = int(os.environ.get("CACHE_TTL_SECONDS", "300"))',
    },
]


def build_few_shot_prompt(task: str) -> list[dict]:
    messages: list[dict] = []
    for ex in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": f"Task: {ex['task']}"})
        messages.append({"role": "assistant", "content": ex["code"]})
    messages.append({"role": "user", "content": f"Task: {task}"})
    return messages


def generate_with_few_shot(task: str) -> str:
    messages = build_few_shot_prompt(task)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "You are a code generator. Use ONLY the environment variable names "
            "shown in the examples. Never invent new names."
        ),
        messages=messages,
    )
    return response.content[0].text


result = generate_with_few_shot("Read the Redis URL for the cache layer")
print(result)
```

**Expected Token Savings:** Few-shot grounding cuts hallucination rate by ~80% with no retry overhead — cheaper per correct output than post-validation retries.

**Environment:** Any project; especially effective when `haiku` is the generation model (less world-knowledge, more pattern-matching).

---

## Option 5 — Tool-use pattern: `list_env_vars` tool forces lookup before generation

**Give the model a `list_env_vars` tool. Instruct it to call the tool before writing any `os.environ` reference.**

```python
import os
import json
import anthropic

client = anthropic.Anthropic()

ENV_TOOL = {
    "name": "list_env_vars",
    "description": (
        "Returns the complete list of environment variable names defined for this project. "
        "ALWAYS call this tool before writing any os.environ or os.getenv reference."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def list_env_vars() -> list[str]:
    """Return canonical var names from .env.example + os.environ."""
    import re
    names: set[str] = set(os.environ.keys())
    try:
        with open(".env.example") as f:
            for line in f:
                m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
                if m:
                    names.add(m.group(1))
    except FileNotFoundError:
        pass
    return sorted(names)


def generate_with_tool(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[ENV_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_use = next(b for b in response.content if b.type == "tool_use")
            tool_result = list_env_vars()

            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(tool_result),
                }],
            })
        else:
            return next(
                b.text for b in response.content if hasattr(b, "text")
            )


result = generate_with_tool(
    "Write a Python config loader that reads our service's environment variables."
)
print(result)
```

**Expected Token Savings:** Model self-grounds by looking up real names before writing code — eliminates all hallucinated var names without any post-processing or retries.

**Environment:** Agents already using tool-use; adds one extra tool call (~200 tokens) per generation request.

---

## Option 6 — Static analysis linter as CI gate

**Run a custom AST linter in CI that fails the build if generated code references undeclared env vars.**

```python
#!/usr/bin/env python3
"""
env_var_linter.py — fail CI if code references unknown env vars.
Usage: python env_var_linter.py generated_code.py [generated_code2.py ...]
"""
import ast
import re
import sys
import os


def load_known_vars(root: str = ".") -> set[str]:
    known: set[str] = set()
    for fname in [".env.example", ".env.test", "docker-compose.yml"]:
        path = os.path.join(root, fname)
        try:
            with open(path) as f:
                for line in f:
                    m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
                    if m:
                        known.add(m.group(1))
        except FileNotFoundError:
            pass
    return known


def extract_env_refs(path: str) -> list[tuple[int, str]]:
    with open(path) as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    refs: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            if (isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                refs.append((node.lineno, node.slice.value))
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "getenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                refs.append((node.lineno, node.args[0].value))
    return refs


def main() -> int:
    known = load_known_vars()
    errors = 0
    for path in sys.argv[1:]:
        for lineno, var in extract_env_refs(path):
            if var not in known:
                print(f"{path}:{lineno}: unknown env var '{var}' (not in .env.example)")
                errors += 1
    if errors:
        print(f"\n{errors} unknown env var reference(s) found. Update .env.example or fix the code.")
    return 1 if errors else 0


sys.exit(main())
```

**CI integration (GitHub Actions):**
```yaml
- name: Lint generated env vars
  run: python scripts/env_var_linter.py $(git diff --name-only HEAD~1 | grep '\.py$')
```

**Expected Token Savings:** Zero extra LLM tokens — the linter runs locally. Prevents entire debug → regenerate cycles (typically 4–10 LLM calls) from reaching production.

**Environment:** Any Python project with CI; requires `.env.example` as the canonical source of truth.

---

## Comparison

| Option | When it Catches Errors | Extra LLM Tokens | Runtime Risk | Complexity |
|--------|----------------------|-----------------|--------------|------------|
| 1. Inject inventory | Generation time | ~50 (inventory) | None | Very Low |
| 2. AST post-validator | Post-generation | ~500/retry | None | Low |
| 3. Pydantic schema | Generation time | ~100 (schema) | None | Low |
| 4. Few-shot grounding | Generation time | ~200 (examples) | None | Low |
| 5. Tool-use lookup | During generation | ~200 (tool call) | None | Medium |
| 6. CI linter gate | CI pipeline | Zero | None | Medium |

**Recommended path:** Use Option 1 (inject inventory) as the baseline — one line change to the system prompt, zero extra API calls. Layer Option 2 (AST validator) for high-stakes code generation. Add Option 6 (CI linter) as a safety net that catches regressions without consuming any tokens.
