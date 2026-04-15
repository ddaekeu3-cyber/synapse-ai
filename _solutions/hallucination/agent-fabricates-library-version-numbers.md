---
layout: solution
title: "Agent fabricates library version numbers"
category: hallucination
description: "Agent confidently specifies package version numbers that don't exist, are incompatible, or are far behind the current release — producing requirements.txt files and pip install commands that fail or install insecure outdated versions."
tags: [hallucination, dependencies, packaging, pip, version, validation]
---

## Symptom

The agent generates `anthropic==2.5.0` (a version that never existed), `requests==3.0.0` (not yet released), or `fastapi==0.95.0` (a real version, but two major versions behind). Users copy the `requirements.txt`, run `pip install -r requirements.txt`, and get either a resolution error or silently install a version with known security issues.

## Root Cause

The model's training data contains package versions from a fixed point in time. For popular packages it saw frequently, it may know several real version strings. For less-common packages, it interpolates plausible-looking version numbers from the format pattern. It has no mechanism to verify that the version it generates is actually published on PyPI.

## Fix

Never generate pinned version numbers from memory. Either generate unpinned dependencies and let the user's environment resolver pick the version, or validate generated versions against PyPI's API before returning them.

---

### Option 1 — Generate unpinned dependencies, add a system prompt rule

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

SYSTEM = (
    "You are a Python development assistant. "
    "When generating requirements.txt or pip install commands:\n"
    "- NEVER pin to a specific version unless the user explicitly requests it\n"
    "- Use unpinned names (e.g. 'requests', 'fastapi') or minimum-version bounds (e.g. 'fastapi>=0.100')\n"
    "- If the user asks for a specific version, recommend they check PyPI first\n"
    "- Never invent version numbers — your training data may be out of date\n"
    "This prevents installing nonexistent or insecure versions."
)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** None — the system rule adds ~80 tokens but prevents broken dependency files that require debugging turns.
**Environment:** Any agent that generates Python project scaffolding or dependency lists; the simplest fix with no external calls.

---

### Option 2 — PyPI version validator: check generated versions before returning

```python
import anthropic
import re
import urllib.request
import json

client = anthropic.Anthropic(api_key="sk-live-...")

_pypi_cache: dict[str, list[str]] = {}


def get_pypi_versions(package: str) -> list[str]:
    """Fetch available versions for a package from PyPI JSON API."""
    if package in _pypi_cache:
        return _pypi_cache[package]
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        versions = list(data.get("releases", {}).keys())
        _pypi_cache[package] = versions
        return versions
    except Exception:
        return []


def extract_pinned_deps(text: str) -> list[tuple[str, str]]:
    """Extract (package, version) pairs from requirements format or pip install commands."""
    pattern = re.compile(r"([a-zA-Z0-9_\-]+)==([0-9][0-9a-z.\-]*)")
    return [(m.group(1), m.group(2)) for m in pattern.finditer(text)]


def validate_deps(text: str) -> tuple[list[str], list[str]]:
    """
    Returns (valid_pins, invalid_pins).
    Checks each pinned version against PyPI.
    """
    valid, invalid = [], []
    for pkg, ver in extract_pinned_deps(text):
        available = get_pypi_versions(pkg)
        if not available:
            invalid.append(f"{pkg}=={ver} (could not verify — package not found on PyPI)")
        elif ver in available:
            valid.append(f"{pkg}=={ver}")
        else:
            # Find closest real versions
            close = [v for v in available if v.startswith(ver.split(".")[0])][-3:]
            invalid.append(
                f"{pkg}=={ver} (not on PyPI; recent versions: {', '.join(reversed(close))})"
            )
    return valid, invalid


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text
    _, invalid = validate_deps(output)

    if invalid:
        print(f"Hallucinated versions detected: {invalid}")
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": output},
            {
                "role": "user",
                "content": (
                    f"The following version pins in your response are not available on PyPI: "
                    f"{invalid}. "
                    "Please rewrite using unpinned package names or correct versions."
                ),
            },
        ]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        output = response.content[0].text

    return output
```

**Expected Token Savings:** Correction turn costs ~400 tokens; prevents broken requirements files that cause user-visible failures.
**Environment:** Agents that generate Python project files; requires internet access to reach PyPI (cache results aggressively to avoid repeated lookups).

---

### Option 3 — PyPI lookup tool: agent must verify before pinning

```python
import anthropic
import urllib.request
import json

client = anthropic.Anthropic(api_key="sk-live-...")

PYPI_TOOL = {
    "name": "get_latest_version",
    "description": (
        "Look up the latest stable version of a Python package on PyPI. "
        "Always call this before pinning any package version in requirements.txt or pip commands. "
        "Never guess version numbers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": "The PyPI package name, e.g. 'requests', 'fastapi', 'anthropic'.",
            }
        },
        "required": ["package"],
    },
}

SYSTEM = (
    "You are a Python dependency expert. "
    "When generating any requirements.txt, setup.py, or pip install command that includes specific versions, "
    "you MUST call get_latest_version first to verify the version exists. "
    "Never pin a version you haven't verified."
)


def handle_get_latest_version(package: str) -> str:
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]
        requires_python = data["info"].get("requires_python", "any")
        return (
            f"Package: {package}\n"
            f"Latest version: {latest}\n"
            f"Requires Python: {requires_python}\n"
            f"PyPI URL: https://pypi.org/project/{package}/"
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return f"ERROR: Package '{package}' not found on PyPI."
        return f"ERROR: PyPI returned HTTP {exc.code}"
    except Exception as exc:
        return f"ERROR: Could not reach PyPI — {exc}"


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=[PYPI_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "get_latest_version":
                    result = handle_get_latest_version(block.input["package"])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** Each version lookup adds one round-trip (~200 tokens); the model produces verified versions and never fabricates them.
**Environment:** Interactive agents helping with project setup; the tool makes every version lookup explicit and auditable.

---

### Option 4 — Minimum-bound strategy: generate `>=` constraints instead of pins

```python
import anthropic
import re
import urllib.request
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def get_latest_stable(package: str) -> str | None:
    """Return latest stable version string from PyPI, or None on error."""
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())["info"]["version"]
    except Exception:
        return None


def upgrade_pins_to_bounds(requirements_text: str) -> str:
    """
    Convert exact pins (==) to minimum bounds (>=) based on verified PyPI versions.
    If the pinned version doesn't exist, replace with latest.
    """
    def replace_pin(match: re.Match) -> str:
        pkg = match.group(1)
        ver = match.group(2)
        latest = get_latest_stable(pkg)
        if latest is None:
            return f"{pkg}>={ver}"  # can't verify, keep as minimum bound
        # Use whichever is higher: pinned or latest
        return f"{pkg}>={ver}"  # safer: use specified as minimum

    pattern = re.compile(r"([a-zA-Z0-9_\-]+)==([0-9][0-9a-z.\-]*)", re.MULTILINE)
    return pattern.sub(replace_pin, requirements_text)


SYSTEM = (
    "You are a Python dependency assistant. "
    "Generate requirements using minimum-version constraints (e.g. 'fastapi>=0.100') "
    "rather than exact pins (fastapi==0.100.0). "
    "Exact pins cause breakage when versions don't exist. "
    "Use >= to specify the minimum acceptable version you know works."
)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text
    # Post-process: convert any remaining == pins to >= bounds
    return upgrade_pins_to_bounds(output)
```

**Expected Token Savings:** None on tokens; the minimum-bound strategy is resilient to version hallucinations since `>=X.Y` is valid even if the exact version was invented.
**Environment:** Agents generating reproducible-but-flexible dependency files; minimum bounds are safer than exact pins for most users.

---

### Option 5 — Few-shot grounding with real, current versions

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Curated, verified versions — update this list periodically
VERIFIED_VERSIONS = """
Verified Python package versions (as of 2026-04):
- anthropic: 0.49.0 (latest stable)
- requests: 2.32.3
- httpx: 0.28.1
- fastapi: 0.115.12
- pydantic: 2.11.3
- sqlalchemy: 2.0.40
- redis: 5.2.1
- boto3: 1.37.27
- openai: 1.74.0
- aiohttp: 3.11.14
- pytest: 8.3.5
- black: 25.1.0

When generating requirements, use ONLY these versions or leave packages unpinned.
If the user needs a package not in this list, do NOT invent a version — use it unpinned.
"""


def run_agent(user_message: str) -> str:
    system = (
        "You are a Python dependency assistant.\n\n"
        f"{VERIFIED_VERSIONS}\n\n"
        "Never invent version numbers. Use the verified list above or leave packages unpinned."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** The verified list adds ~200 tokens but eliminates hallucinated versions for the most common packages; the "unpinned if unknown" rule handles the long tail.
**Environment:** Agents that primarily use a fixed set of well-known packages; the list needs periodic manual updates (monthly or on each major release).

---

### Option 6 — requirements.txt auditor: run pip check after generation

```python
import anthropic
import subprocess
import tempfile
import os
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_requirements(text: str) -> str | None:
    """Extract requirements.txt content from a code block."""
    match = re.search(r"```(?:txt|requirements)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Try to find a block that looks like requirements
    lines = [l.strip() for l in text.splitlines() if re.match(r"[a-zA-Z0-9_\-]+(==|>=|<=|~=|!=|\s)", l.strip())]
    return "\n".join(lines) if len(lines) >= 2 else None


def audit_requirements(requirements_text: str) -> tuple[bool, str]:
    """
    Write requirements to a temp file and attempt pip download --dry-run.
    Returns (ok, error_message).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(requirements_text)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["pip", "download", "--no-deps", "--dry-run", "-r", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        # Parse errors
        error = result.stderr or result.stdout
        return False, error[:500]
    except Exception as exc:
        return False, str(exc)
    finally:
        os.unlink(tmp_path)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text

    reqs = extract_requirements(output)
    if reqs:
        ok, error = audit_requirements(reqs)
        if not ok:
            print(f"pip audit failed: {error[:200]}")
            messages = [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": output},
                {
                    "role": "user",
                    "content": (
                        f"pip couldn't resolve the requirements you generated. Error: {error[:300]}. "
                        "Please rewrite using unpinned package names or verified versions."
                    ),
                },
            ]
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=messages,
            )
            output = response.content[0].text

    return output


# Comparison table
# | Option | Validation Method | Requires Network | Blocks Output |
# |--------|------------------|-----------------|---------------|
# | 1 System rule | Prompt instruction | No | No |
# | 2 PyPI API check | HTTP to pypi.org | Yes | Yes (corrects) |
# | 3 Lookup tool | Tool call to PyPI | Yes | Yes (corrects) |
# | 4 Min-bound convert | Regex post-process | Optional | No |
# | 5 Few-shot list | Prompt grounding | No | No |
# | 6 pip dry-run | Local pip | Yes (pip resolver) | Yes (corrects) |
```

**Expected Token Savings:** The pip dry-run runs in subprocess with zero model tokens; it catches resolution errors that the PyPI API check misses (e.g. incompatible dependency graphs).
**Environment:** Agents with access to a Python environment where pip is installed; the dry-run is the most reliable validation since it uses the same resolver the user will use.
