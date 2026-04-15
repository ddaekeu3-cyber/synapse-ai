---
layout: solution
title: "Agent Hallucinates Configuration File Syntax"
category: hallucination
description: "Agent generates plausible-looking but syntactically invalid YAML, TOML, or JSON config for libraries it doesn't know well, causing silent failures or cryptic parse errors."
tags: [hallucination, configuration, yaml, toml, validation, reliability]
---

## Symptom

The agent generates a `pyproject.toml` with `[tool.mypy]` fields that don't exist, a Kubernetes YAML with invalid field names, or a Docker Compose file with keys that were renamed in a newer spec version. The config passes a quick visual inspection but fails at runtime with an error like `Unknown field: 'strictNullChecks'` or silently ignores the invalid block. The developer loses time debugging a file that looks correct.

## Root Cause

LLMs learn config syntax from training data that may be outdated, incomplete, or from forum posts with bugs. The model generates config that *looks* right based on statistical patterns — similar keys appear near each other, the structure matches the format — but the specific field names, value types, or nesting levels are wrong. Without a validation step, the agent has no feedback signal that the generated config is invalid.

## Fix

### Option 1 — Inject the official schema into the prompt

```python
import json
import anthropic

client = anthropic.Anthropic()

# Fetch the JSON Schema for the config you want to generate
# Many tools publish official schemas: schemastore.org has 500+ schemas
MYPY_SCHEMA_EXCERPT = {
    "type": "object",
    "properties": {
        "python_version": {"type": "string", "description": "Python version for type checks (e.g. '3.11')"},
        "strict":         {"type": "boolean"},
        "ignore_missing_imports": {"type": "boolean"},
        "disallow_untyped_defs":  {"type": "boolean"},
        "warn_return_any":        {"type": "boolean"},
    },
    "additionalProperties": False,
}

def generate_mypy_config(project_description: str) -> str:
    schema_str = json.dumps(MYPY_SCHEMA_EXCERPT, indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Generate a [tool.mypy] section for pyproject.toml.\n\n"
                f"Project: {project_description}\n\n"
                f"IMPORTANT: Only use fields from this exact schema — no others:\n"
                f"```json\n{schema_str}\n```\n\n"
                f"Return only the TOML content, no explanation."
            ),
        }],
    )
    return response.content[0].text

config = generate_mypy_config("Async FastAPI service, Python 3.11, strict type checking required.")
print(config)

# Validate the generated config against the schema
try:
    import tomllib  # Python 3.11+
    parsed = tomllib.loads(f"[tool.mypy]\n{config.replace('[tool.mypy]', '').strip()}")
    tool_mypy = parsed.get("tool", {}).get("mypy", parsed)
    import jsonschema
    jsonschema.validate(tool_mypy, MYPY_SCHEMA_EXCERPT)
    print("[validation] config is valid")
except Exception as e:
    print(f"[validation] FAILED: {e}")
```

**Expected Token Savings:** Schema injection prevents hallucinated fields that would require a debugging cycle; catching errors before deployment saves the debugging tokens and human time.
**Environment:** Any agent generating config for tools with published JSON Schemas (mypy, eslint, GitHub Actions, Kubernetes, Docker Compose).

---

### Option 2 — Validate generated YAML/TOML/JSON before returning

```python
import anthropic
import yaml
import json

client = anthropic.Anthropic()

KNOWN_DOCKER_COMPOSE_KEYS = {
    "version", "services", "networks", "volumes", "configs", "secrets",
}
KNOWN_SERVICE_KEYS = {
    "image", "build", "command", "entrypoint", "environment", "env_file",
    "ports", "volumes", "networks", "depends_on", "restart", "labels",
    "healthcheck", "deploy", "user", "working_dir", "expose", "stdin_open",
    "tty", "ulimits", "mem_limit", "cpu_shares",
}

def validate_compose(compose_yaml: str) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []
    try:
        doc = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    if not isinstance(doc, dict):
        return ["Top-level must be a YAML mapping"]

    unknown_top = set(doc.keys()) - KNOWN_DOCKER_COMPOSE_KEYS
    if unknown_top:
        errors.append(f"Unknown top-level keys: {unknown_top}")

    for svc_name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            errors.append(f"Service '{svc_name}' must be a mapping")
            continue
        unknown_svc = set(svc.keys()) - KNOWN_SERVICE_KEYS
        if unknown_svc:
            errors.append(f"Service '{svc_name}' has unknown keys: {unknown_svc}")
        if "ports" in svc:
            for port in svc["ports"]:
                if not isinstance(port, (str, int, dict)):
                    errors.append(f"Service '{svc_name}' port entry is not a string or mapping")
    return errors

def generate_and_validate_compose(requirements: str, max_attempts: int = 3) -> str:
    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a docker-compose.yml for: {requirements}\n\n"
                    "Use only standard Docker Compose v3 keys. Return only the YAML."
                    + (f"\n\nPrevious attempt had errors — fix them:\n{'; '.join(_last_errors)}"
                       if attempt > 0 else "")
                ),
            }],
        )
        compose_text = response.content[0].text
        _last_errors = validate_compose(compose_text)
        if not _last_errors:
            print(f"[validation] attempt {attempt+1}: valid")
            return compose_text
        print(f"[validation] attempt {attempt+1}: {len(_last_errors)} errors — retrying")
    raise RuntimeError(f"Could not generate valid config after {max_attempts} attempts")

result = generate_and_validate_compose("Python FastAPI app on port 8000 with PostgreSQL 15")
print(result[:300])
```

**Expected Token Savings:** Catching syntax errors in 1–2 retry turns is far cheaper than a developer manually debugging a broken deployment; the retry loop converges quickly because the error message is precise.
**Environment:** CI/CD pipelines that auto-generate config; agents that deploy infrastructure.

---

### Option 3 — Use structured outputs to constrain config fields

```python
import anthropic
import json

client = anthropic.Anthropic()

# Define the config as a strict JSON Schema — only valid fields are generated
GITHUB_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "on": {
            "type": "object",
            "properties": {
                "push":        {"type": "object"},
                "pull_request":{"type": "object"},
                "schedule":    {"type": "array"},
            },
        },
        "jobs": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "runs-on": {"type": "string"},
                    "steps":   {"type": "array"},
                    "needs":   {"type": ["string", "array"]},
                    "if":      {"type": "string"},
                },
                "required": ["runs-on", "steps"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "on", "jobs"],
    "additionalProperties": False,
}

def generate_github_actions(pipeline_desc: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Generate a GitHub Actions workflow JSON object for: {pipeline_desc}\n\n"
                f"You MUST conform to this schema exactly:\n"
                f"{json.dumps(GITHUB_ACTIONS_SCHEMA, indent=2)}\n\n"
                "Return only valid JSON, no markdown fences."
            ),
        }],
    )
    workflow = json.loads(response.content[0].text)

    # Validate against schema
    import jsonschema
    jsonschema.validate(workflow, GITHUB_ACTIONS_SCHEMA)
    print("[validation] GitHub Actions workflow is schema-valid")
    return workflow

workflow = generate_github_actions("Python pytest CI on push to main, runs on ubuntu-latest")
import yaml
print(yaml.dump(workflow, default_flow_style=False))
```

**Expected Token Savings:** Schema-constrained generation eliminates the most common hallucination class (wrong field names); schema validation is a zero-cost post-processing step.
**Environment:** Any agent generating structured config for tools with machine-readable schemas; especially useful for YAML formats (GitHub Actions, Kubernetes, Helm) that can be represented as JSON first.

---

### Option 4 — Few-shot examples from real, validated configs

```python
import anthropic

client = anthropic.Anthropic()

# Real, validated config snippets used as few-shot examples
FEW_SHOT_EXAMPLES = [
    {
        "description": "Strict mypy config for a web service",
        "config": """\
[tool.mypy]
python_version = "3.11"
strict = true
ignore_missing_imports = true
warn_return_any = true""",
    },
    {
        "description": "Relaxed mypy config for a data science notebook project",
        "config": """\
[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
disallow_untyped_defs = false""",
    },
]

def generate_mypy_with_examples(project_desc: str) -> str:
    examples_str = "\n\n".join(
        f"Example ({ex['description']}):\n```toml\n{ex['config']}\n```"
        for ex in FEW_SHOT_EXAMPLES
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Generate a [tool.mypy] TOML config block. "
                "Use ONLY the field names shown in the examples below — no others.\n\n"
                f"{examples_str}\n\n"
                f"Now generate for: {project_desc}\n"
                "Return only the TOML block."
            ),
        }],
    )
    return response.content[0].text

config = generate_mypy_with_examples(
    "Async FastAPI service with strict type checking, Python 3.12"
)
print(config)
```

**Expected Token Savings:** Few-shot examples from real configs are the cheapest form of hallucination prevention — no schema fetching, no validation library needed; works for formats without published JSON Schemas.
**Environment:** Less common config formats (custom tool configs, in-house frameworks); any config where the field names are stable and few.

---

### Option 5 — Post-generate diff against known-good template

```python
import anthropic
import difflib

client = anthropic.Anthropic()

# Known-good base template with all valid keys (values are placeholders)
KNOWN_GOOD_PYPROJECT = """\
[tool.ruff]
line-length = 88
target-version = "py311"
select = ["E", "W", "F", "I"]
ignore = []
fixable = ["ALL"]

[tool.ruff.per-file-ignores]
"tests/**/*.py" = ["S101"]

[tool.ruff.isort]
known-first-party = ["myapp"]
"""

VALID_RUFF_KEYS = {
    "line-length", "target-version", "select", "ignore", "fixable",
    "per-file-ignores", "isort", "mccabe", "pydocstyle", "flake8-quotes",
}

def check_for_hallucinated_keys(generated: str, valid_keys: set) -> list[str]:
    """Extract keys from a TOML section and flag unknown ones."""
    hallucinated = []
    for line in generated.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("[") and not line.startswith("#"):
            key = line.split("=")[0].strip()
            if key and key not in valid_keys:
                hallucinated.append(key)
    return hallucinated

def generate_ruff_config(requirements: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Generate a [tool.ruff] section for pyproject.toml for: {requirements}\n\n"
                f"Base your output on this known-good template:\n```toml\n{KNOWN_GOOD_PYPROJECT}\n```\n\n"
                "Return only the modified TOML section."
            ),
        }],
    )
    generated = response.content[0].text

    hallucinated = check_for_hallucinated_keys(generated, VALID_RUFF_KEYS)
    if hallucinated:
        print(f"[hallucination] unknown keys detected: {hallucinated}")
        print("[hallucination] falling back to template")
        return KNOWN_GOOD_PYPROJECT

    print("[validation] all keys are valid ruff keys")

    # Show diff vs template
    diff = list(difflib.unified_diff(
        KNOWN_GOOD_PYPROJECT.splitlines(),
        generated.splitlines(),
        lineterm="",
    ))
    if diff:
        print("[diff] changes from template:")
        for line in diff[:20]:
            print(f"  {line}")

    return generated

result = generate_ruff_config("Strict Python 3.12 project with import sorting and docstring style")
print(result)
```

**Expected Token Savings:** Template-diff approach catches hallucinated keys in O(n) time with zero extra API calls; falling back to the template guarantees a working config on first failure.
**Environment:** Internal tooling agents; teams with established config standards where deviation from the template is suspicious.

---

### Option 6 — Run the generated config through the actual tool's parser

```python
import anthropic
import subprocess
import tempfile
import os

client = anthropic.Anthropic()

def generate_nginx_config(site_spec: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Generate a minimal nginx server block for: {site_spec}\n\n"
                "Return only the nginx configuration, no markdown."
            ),
        }],
    )
    return response.content[0].text

def validate_nginx_config(config_text: str) -> tuple[bool, str]:
    """Run nginx -t to validate the config syntax."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
        f.write(config_text)
        config_path = f.name
    try:
        result = subprocess.run(
            ["nginx", "-t", "-c", config_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, "nginx -t: syntax OK"
        else:
            return False, result.stderr.strip()
    except FileNotFoundError:
        return None, "nginx not installed — skipping live validation"
    except subprocess.TimeoutExpired:
        return None, "nginx -t timed out"
    finally:
        os.unlink(config_path)

def generate_validated_nginx(site_spec: str, max_attempts: int = 3) -> str:
    last_error = ""
    for attempt in range(max_attempts):
        config = generate_nginx_config(
            site_spec + (f"\n\nFix this error from last attempt: {last_error}" if last_error else "")
        )
        ok, msg = validate_nginx_config(config)
        print(f"[attempt {attempt+1}] {msg}")
        if ok is True:
            return config
        if ok is None:
            print("[fallback] skipping live validation — returning generated config")
            return config
        last_error = msg
    raise RuntimeError(f"Could not generate valid nginx config after {max_attempts} attempts")

config = generate_validated_nginx("Serve a React SPA at /app, reverse proxy /api to localhost:8000")
print(config[:400])
```

**Expected Token Savings:** Running the real parser catches subtle syntax errors that schema checks miss (wrong directive placement, missing semicolons, invalid parameter counts); 1–2 retry turns with the exact error message converges faster than human debugging.
**Environment:** Agents generating configs for tools that have a `--check` or `--dry-run` mode (nginx, HAProxy, Terraform validate, kubeval, helm lint); most infrastructure tools support syntax validation.

---

## Comparison

| Option | Validation Method | Extra API Calls | Catches Field Hallucinations | Catches Syntax Errors | Best For |
|---|---|---|---|---|---|
| 1. Schema in prompt | Prompt constraint | 0 | Yes | Partial | Tools with published JSON Schemas |
| 2. Post-generate validation | In-process check | 0–2 (retry) | Yes | Yes (YAML parse) | Docker Compose, structured formats |
| 3. Structured output schema | Schema-constrained gen | 0 | Yes (by design) | N/A | JSON-representable configs |
| 4. Few-shot examples | Prompt constraint | 0 | Partial | No | Formats without schemas; stable fields |
| 5. Template diff | In-process check | 0 | Yes | No | Teams with canonical templates |
| 6. Real parser validation | External CLI | 0–2 (retry) | Yes | Yes (full parse) | nginx, Terraform, Helm, kubeval |
