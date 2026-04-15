---
layout: solution
title: "Agent Generates Code Without Dependency Pinning"
category: general
description: "Agent generates requirements.txt, package.json, or pyproject.toml with unpinned or loosely-pinned dependencies — code that works today breaks silently when a transitive dependency releases a breaking change."
tags: [reliability, dependencies, pinning, reproducibility, code-generation]
---

## Symptom

The agent generates a `requirements.txt` like this:

```
requests
anthropic
fastapi
pydantic
```

Three months later, `pydantic` releases v3 with breaking changes. The deployment pipeline installs the latest version automatically, and the application crashes at startup. The agent-generated code never specified which version it was written for.

## Root Cause

The model generates dependency lists based on package names it knows but doesn't know which specific version was current at generation time, and doesn't know the user's stability requirements:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    messages=[{"role": "user", "content": "Generate a requirements.txt for a FastAPI app with Anthropic SDK"}]
)
# → "requests\nanthropicfastapi\npydantic" — no versions
```

---

## Fix

### Option 1 — Inject installed package versions into the prompt

Read the current environment's installed packages and tell the model exactly which versions to pin.

```python
import anthropic
import subprocess
import sys

client = anthropic.Anthropic(api_key="sk-live-...")


def get_installed_versions() -> dict[str, str]:
    """Get installed package versions from the current Python environment."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        capture_output=True, text=True
    )
    import json
    packages = json.loads(result.stdout)
    return {p["name"].lower(): p["version"] for p in packages}


def generate_pinned_requirements(description: str) -> str:
    installed = get_installed_versions()

    # Format a subset of relevant packages as context
    common_packages = [
        "anthropic", "fastapi", "uvicorn", "pydantic", "requests", "httpx",
        "sqlalchemy", "asyncpg", "redis", "celery", "boto3", "openai",
        "numpy", "pandas", "pillow", "aiohttp", "tenacity"
    ]
    versions_context = "\n".join(
        f"  {pkg}=={installed[pkg]}"
        for pkg in common_packages
        if pkg in installed
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""You are a Python dependency expert.
When generating requirements.txt, always pin exact versions (==) for direct dependencies.
Use these currently-installed versions as the source of truth:

{versions_context}

Rules:
1. Use == for all direct dependencies (not >=, ~=, or unpinned).
2. Only include packages actually needed for the described app.
3. Do not include development/test dependencies unless asked.
4. Add a comment explaining the Python version these were tested with.""",
        messages=[{"role": "user", "content": f"Generate requirements.txt for: {description}"}]
    )
    return response.content[0].text.strip()


req = generate_pinned_requirements("FastAPI REST API with Anthropic SDK and PostgreSQL async")
print(req)
# → # Tested with Python 3.12
#   anthropic==0.40.0
#   fastapi==0.115.0
#   asyncpg==0.29.0
#   pydantic==2.10.0

# Expected Token Savings: pinned deps prevent broken installs → no debugging sessions
# Environment: code generation agents producing production-ready Python projects
```

---

### Option 2 — Post-generation version resolution

After generating the dependency list, resolve each package to its latest stable version using PyPI API and rewrite with pinned versions.

```python
import anthropic
import re
import urllib.request
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def get_latest_version(package: str) -> str | None:
    """Fetch latest stable version from PyPI."""
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def pin_requirements(raw_requirements: str) -> str:
    """Take an unpinned requirements.txt and pin all packages to latest stable."""
    lines = raw_requirements.strip().splitlines()
    pinned_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pinned_lines.append(line)
            continue

        # Extract package name (strip existing version specifiers)
        pkg_name = re.split(r'[>=<!~\[]', stripped)[0].strip()

        if "==" in stripped:
            pinned_lines.append(line)  # Already pinned — keep
            continue

        version = get_latest_version(pkg_name)
        if version:
            pinned_lines.append(f"{pkg_name}=={version}")
            print(f"Pinned: {pkg_name}=={version}")
        else:
            pinned_lines.append(line)  # Couldn't resolve — keep as-is
            print(f"Warning: could not resolve version for {pkg_name}")

    return "\n".join(pinned_lines)


def generate_and_pin(description: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"List Python package names needed for: {description}\nReturn one package per line, no versions."}]
    )
    raw = response.content[0].text.strip()
    pinned = pin_requirements(raw)
    return pinned


result = generate_and_pin("async FastAPI app with Anthropic SDK and Redis caching")
print("\nFinal requirements.txt:")
print(result)

# Expected Token Savings: correct pinned versions from the start vs debugging broken installs
# Environment: any agent generating Python project files; requires internet access for PyPI lookup
```

---

### Option 3 — Generate with explicit version ranges and lockfile recommendation

When exact versions can't be determined, generate conservative version ranges and always include a lockfile instruction.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

PINNING_SYSTEM = """You are a Python packaging expert. When generating dependency files:

1. For requirements.txt: use >= with an upper bound, never bare package names.
   Example: fastapi>=0.110.0,<1.0.0
2. Always add a comment: "Run: pip install -r requirements.txt && pip freeze > requirements.lock"
3. For pyproject.toml: use [tool.poetry.dependencies] with ^ or ~ bounds.
4. Always recommend generating a lockfile (pip freeze, poetry.lock, uv.lock).
5. Separate: direct dependencies (pinned) vs dev dependencies (separate block).
6. Include Python version constraint: python_requires=">=3.11"

Never use bare package names without version specifiers."""


def generate_with_ranges(description: str, format_type: str = "requirements.txt") -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=PINNING_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Generate a {format_type} for: {description}"
        }]
    )
    return response.content[0].text.strip()


# requirements.txt with ranges
print(generate_with_ranges(
    "FastAPI API with async SQLAlchemy, Anthropic SDK, and JWT auth",
    "requirements.txt"
))

# pyproject.toml format
print(generate_with_ranges(
    "CLI tool using Click and Rich for output",
    "pyproject.toml [tool.poetry] section"
))

# Expected Token Savings: conservative bounds prevent most breaking changes; lockfile handles the rest
# Environment: projects that must balance reproducibility with receiving security patches
```

---

### Option 4 — Package.json with exact versions for Node.js agents

For JavaScript/TypeScript agents, generate `package.json` with exact versions and a lockfile recommendation.

```python
import anthropic
import json
import urllib.request

client = anthropic.Anthropic(api_key="sk-live-...")


def get_npm_latest(package: str) -> str | None:
    """Fetch latest version from npm registry."""
    try:
        url = f"https://registry.npmjs.org/{package}/latest"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("version")
    except Exception:
        return None


NODE_PINNING_SYSTEM = """You are a Node.js packaging expert.

Rules for package.json generation:
1. Use exact versions (no ^ or ~) for production dependencies.
2. Use ^version for devDependencies (patch/minor updates OK for dev tools).
3. Always include "engines": {"node": ">=20.0.0"} or appropriate version.
4. Include a "scripts" block with start, build, test commands.
5. Add a comment field explaining: "Run: npm ci (not npm install) for reproducible installs"
6. Never use "latest" as a version specifier.

Example production dep: "express": "4.18.2" (exact)
Example dev dep: "typescript": "^5.3.0" (caret OK for dev)"""


def generate_package_json(description: str, resolve_versions: bool = True) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=NODE_PINNING_SYSTEM,
        messages=[{"role": "user", "content": f"Generate package.json for: {description}"}]
    )
    generated = response.content[0].text.strip()

    if not resolve_versions:
        return generated

    # Extract and resolve package versions
    try:
        # Strip markdown code blocks if present
        clean = generated
        if "```" in clean:
            lines = clean.split("\n")
            clean = "\n".join(l for l in lines if not l.startswith("```"))

        pkg = json.loads(clean)

        for dep_section in ("dependencies", "devDependencies"):
            if dep_section not in pkg:
                continue
            for name in list(pkg[dep_section].keys()):
                current = pkg[dep_section][name]
                if current in ("latest", ""):
                    latest = get_npm_latest(name)
                    if latest:
                        pkg[dep_section][name] = latest
                        print(f"Resolved {name}: {latest}")

        return json.dumps(pkg, indent=2)
    except (json.JSONDecodeError, KeyError):
        return generated


print(generate_package_json("Express.js REST API with TypeScript and Jest testing"))

# Expected Token Savings: pinned deps in package.json + npm ci = reproducible installs always
# Environment: code generation agents producing Node.js/TypeScript projects
```

---

### Option 5 — Validate generated dependency file before returning

Parse and lint the generated dependency file, rejecting it if it contains unpinned packages.

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class DependencyLintResult:
    ok: bool
    unpinned: list[str]
    pinned: list[str]
    warnings: list[str]


def lint_requirements_txt(content: str) -> DependencyLintResult:
    unpinned = []
    pinned = []
    warnings = []

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue

        pkg_name = re.split(r'[>=<!~\[\s@]', line)[0].strip()

        if "==" in line:
            pinned.append(pkg_name)
        elif ">=" in line and "<" in line:
            pinned.append(pkg_name)  # Range with upper bound — acceptable
            warnings.append(f"{pkg_name}: range bound (consider pinning exactly)")
        elif ">=" in line:
            unpinned.append(pkg_name)  # Lower bound only — can drift up
        elif "~=" in line:
            warnings.append(f"{pkg_name}: compatible release (~=) may drift")
            pinned.append(pkg_name)
        else:
            unpinned.append(pkg_name)  # Bare name — worst case

    return DependencyLintResult(
        ok=len(unpinned) == 0,
        unpinned=unpinned,
        pinned=pinned,
        warnings=warnings,
    )


def generate_pinned_deps(description: str) -> str:
    system = """Generate requirements.txt with == pinned versions for all packages.
Today's date context: 2026-04-15. Use recent but stable package versions.
Format: package_name==X.Y.Z, one per line, with a # Python version comment at top."""

    for attempt in range(3):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": f"requirements.txt for: {description}"}]
        )
        content = response.content[0].text.strip()

        result = lint_requirements_txt(content)

        if result.ok:
            if result.warnings:
                print(f"[warn] {result.warnings}")
            return content

        print(f"[attempt {attempt+1}] {len(result.unpinned)} unpinned packages: {result.unpinned}")
        system += f"\n\nPrevious attempt had unpinned packages: {result.unpinned}. Pin them with ==."

    return content  # Return best effort


req = generate_pinned_deps("async web scraper with httpx, BeautifulSoup4, and SQLite storage")
print(req)

# Expected Token Savings: linter catches unpinned deps before they reach CI/CD
# Environment: CI pipeline validation step for generated dependency files
```

---

### Option 6 — Conda environment.yml with both Python and system dependencies

For data science / ML agents, generate `environment.yml` with pinned Python and system-level packages.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CONDA_SYSTEM = """You are a Python data science environment expert.

Generate conda environment.yml files with:
1. Exact Python version: python=3.12.3 (not python>=3.12)
2. Conda packages with exact versions from conda-forge channel.
3. Pip dependencies (if no conda package exists) also pinned.
4. Channels listed in priority order: conda-forge, defaults.
5. Include a comment: "Create with: conda env create -f environment.yml"
6. Include a comment: "Freeze with: conda env export --no-builds > environment.lock.yml"

Structure:
name: project-name
channels:
  - conda-forge
  - defaults
dependencies:
  - python=X.Y.Z
  - package=X.Y.Z  # conda packages
  - pip:
    - package==X.Y.Z  # pip-only packages"""


def generate_conda_env(description: str, env_name: str = "myenv") -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=CONDA_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Generate environment.yml named '{env_name}' for: {description}"
        }]
    )
    return response.content[0].text.strip()


env = generate_conda_env(
    "Machine learning project with PyTorch, scikit-learn, pandas, and Jupyter",
    env_name="ml-project"
)
print(env)

# Expected Token Savings: conda env with pinned versions = reproducible ML experiments across team
# Environment: data science / ML agents generating reproducible research environments
```

---

## Comparison

| Option | Version Source | Resolves Live | Format | Validation | Complexity |
|--------|---------------|---------------|--------|------------|------------|
| 1 | Current env | No (env) | requirements.txt | No | Low |
| 2 | PyPI API | Yes | requirements.txt | No | Medium |
| 3 | Model knowledge | No | Any | No | Low |
| 4 | npm registry | Yes | package.json | No | Medium |
| 5 | Model + lint | No | requirements.txt | Yes | Low |
| 6 | Model knowledge | No | environment.yml | No | Low |

**Recommended starting point:** Option 1 (inject current env versions) for any agent running in a dev environment — the most accurate source of version info is the environment the developer is already using. Add Option 5's linter as a post-generation gate to catch any unpinned packages. Use Option 2 for agents generating files for a fresh environment where no existing installation exists as reference.
