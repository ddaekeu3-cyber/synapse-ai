---
layout: solution
title: "Agent Hallucinates File System Paths on Different OS"
category: hallucination
description: "Agent generates hardcoded OS-specific paths (C:\\Users\\..., /home/ubuntu/..., /var/lib/...) that don't exist on the target system — code that works in the demo fails on the customer's machine or in the deployment container."
tags: [hallucination, file-paths, cross-platform, portability, os]
---

## Symptom

The agent generates code or configuration that uses hardcoded paths:

```python
# Agent-generated code
config_path = "C:\\Users\\Administrator\\AppData\\Roaming\\myapp\\config.json"
log_dir     = "/home/ubuntu/logs"
data_dir    = "/var/lib/myapp/data"
```

On the actual deployment target (Docker container, macOS dev machine, Windows Server), none of these paths exist. The generated code fails immediately at runtime.

## Root Cause

The model learned common path patterns from training data (Stack Overflow answers, tutorials, GitHub repos) without understanding which environment the user is targeting. It defaults to the most common pattern it has seen for a given OS name, which may not match:

- The user's actual username
- The deployment environment (container, cloud VM, on-premise)
- The OS version or distribution

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: ask for paths without context
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    messages=[{"role": "user", "content": "Write Python code to save a config file on Windows"}]
)
# → Model generates: C:\Users\Administrator\AppData\... (wrong username, wrong path)
```

---

## Fix

### Option 1 — Inject runtime OS context into the prompt

Before generating code with paths, tell the model the actual OS, user, and home directory. The model produces correct paths for the specific environment.

```python
import anthropic
import os
import platform
import sys

client = anthropic.Anthropic(api_key="sk-live-...")


def os_context() -> str:
    """Collect actual runtime environment facts."""
    return f"""Target environment:
- OS: {platform.system()} {platform.release()} ({sys.platform})
- Home directory: {os.path.expanduser('~')}
- Current user: {os.environ.get('USER') or os.environ.get('USERNAME', 'unknown')}
- Python executable: {sys.executable}
- Working directory: {os.getcwd()}
- PATH separator: {os.pathsep}
- Directory separator: {os.sep}"""


def ask_with_os_context(task: str) -> str:
    context = os_context()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""You are a code assistant. When generating file paths, use only:
1. The exact paths shown in the environment context below.
2. Python's pathlib.Path or os.path for portability.
3. Never hardcode usernames, home directories, or distribution-specific paths.

{context}""",
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text.strip()


code = ask_with_os_context("Write Python code to save a JSON config file in the user's home directory")
print(code)
# → Uses Path.home() / '.config' / 'myapp' / 'config.json'  ← portable, correct

# Expected Token Savings: correct code first time → no debugging follow-up turns
# Environment: developer tools, code generation agents deployed across mixed OS fleets
```

---

### Option 2 — Enforce `pathlib.Path` and cross-platform APIs in output

Instruct the model to always use `pathlib.Path`, `os.path`, and standard library functions instead of string literals. Validate the output with a regex scan.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

HARDCODED_PATH_PATTERNS = [
    r'["\']C:\\\\',          # Windows absolute: "C:\
    r'["\']C:/',             # Windows absolute: "C:/
    r'["\']\/home\/\w+\/',   # Linux home: "/home/username/
    r'["\']\/Users\/\w+\/',  # macOS home: "/Users/username/
    r'["\']\/var\/lib\/',    # Linux system: "/var/lib/
    r'["\']\/etc\/\w',       # Linux config: "/etc/
]

PORTABLE_PATH_SYSTEM = """Generate cross-platform Python code.

Rules for file paths (mandatory):
1. Use pathlib.Path — never raw string paths.
2. Use Path.home() instead of hardcoded home directories.
3. Use Path.cwd() for current directory.
4. Use platformdirs or appdirs for app data locations (config, logs, cache).
5. Never use string literals containing 'C:\\\\', '/home/', '/Users/', '/var/lib/'.
6. Use os.environ.get() for environment variables instead of hardcoded values.

Example:
BAD:  config = "/home/ubuntu/.config/myapp/config.json"
GOOD: config = Path.home() / ".config" / "myapp" / "config.json"

BAD:  log_dir = "C:\\Users\\Admin\\AppData\\Local\\Logs"
GOOD: import platformdirs; log_dir = Path(platformdirs.user_log_dir("myapp", "MyCompany"))"""


def find_hardcoded_paths(code: str) -> list[str]:
    found = []
    for pattern in HARDCODED_PATH_PATTERNS:
        matches = re.findall(pattern, code)
        found.extend(matches)
    return found


def generate_portable_code(task: str) -> str:
    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=PORTABLE_PATH_SYSTEM,
            messages=[{"role": "user", "content": task}]
        )
        code = response.content[0].text.strip()

        hardcoded = find_hardcoded_paths(code)
        if not hardcoded:
            return code

        print(f"[attempt {attempt+1}] Found hardcoded paths: {hardcoded} — retrying")
        task = task + f"\n\nPrevious attempt contained hardcoded paths: {hardcoded}. Fix them using pathlib.Path."

    return code  # Return best effort after retries


code = generate_portable_code("Write code to find and read a user's SSH config file")
print(code)

# Expected Token Savings: correct portable code eliminates 2-3 debugging turns per generated snippet
# Environment: code generation agents targeting multi-platform deployment
```

---

### Option 3 — Use `platformdirs` to generate canonical paths per platform

Inject the output of `platformdirs` into the prompt so the model uses the correct, OS-specific standard directories.

```python
import anthropic
import sys

client = anthropic.Anthropic(api_key="sk-live-...")

try:
    import platformdirs
    _HAS_PLATFORMDIRS = True
except ImportError:
    _HAS_PLATFORMDIRS = False


def get_canonical_dirs(app_name: str = "myapp", app_author: str = "MyCompany") -> str:
    """Get OS-appropriate standard directory paths for this environment."""
    if _HAS_PLATFORMDIRS:
        dirs = platformdirs.PlatformDirs(app_name, app_author)
        return f"""Standard directories for {app_name} on {sys.platform}:
- User config:  {dirs.user_config_dir}
- User data:    {dirs.user_data_dir}
- User cache:   {dirs.user_cache_dir}
- User log:     {dirs.user_log_dir}
- Site config:  {dirs.site_config_dir}
- Site data:    {dirs.site_data_dir}"""
    else:
        import os
        from pathlib import Path
        home = Path.home()
        return f"""Standard directories on {sys.platform}:
- Config: {home / '.config' / app_name}
- Data:   {home / '.local' / 'share' / app_name}
- Cache:  {home / '.cache' / app_name}
- Logs:   {home / '.local' / 'state' / app_name / 'log'}"""


def ask_with_dirs(task: str, app_name: str = "myapp") -> str:
    dirs_context = get_canonical_dirs(app_name)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""You are a code assistant. When the task involves file paths, use ONLY
the directories listed in the context below. Use pathlib.Path for all path operations.

{dirs_context}""",
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text.strip()


code = ask_with_dirs("Write code to set up the app's config and log directories")
print(code)
# → Uses exact platformdirs paths appropriate for the running OS

# Expected Token Savings: correct paths on first generation; install: pip install platformdirs
# Environment: desktop apps and daemons that need XDG-compliant or OS-standard directories
```

---

### Option 4 — Container-aware path generation

For containerised agents, detect the environment (Docker, Kubernetes) and inject the appropriate mounted paths and volume conventions.

```python
import anthropic
import os
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")


def detect_container_env() -> dict[str, str]:
    """Detect whether running in a container and return canonical mount points."""
    env = {
        "is_docker": Path("/.dockerenv").exists(),
        "is_k8s": bool(os.environ.get("KUBERNETES_SERVICE_HOST")),
        "data_dir": os.environ.get("DATA_DIR", "/data"),
        "config_dir": os.environ.get("CONFIG_DIR", "/config"),
        "log_dir": os.environ.get("LOG_DIR", "/var/log/app"),
        "tmp_dir": os.environ.get("TMPDIR", "/tmp"),
        "secrets_dir": os.environ.get("SECRETS_DIR", "/run/secrets"),
    }
    return env


def ask_container_paths(task: str) -> str:
    env = detect_container_env()

    if env["is_docker"] or env["is_k8s"]:
        context = f"""Container environment detected.
Use these canonical mount points (configured via environment variables):
- Data:    {env['data_dir']}          (DATA_DIR)
- Config:  {env['config_dir']}        (CONFIG_DIR)
- Logs:    {env['log_dir']}           (LOG_DIR)
- Temp:    {env['tmp_dir']}           (TMPDIR)
- Secrets: {env['secrets_dir']}       (SECRETS_DIR)

Rules:
1. Read all paths from environment variables, not hardcoded strings.
2. Use pathlib.Path(os.environ.get('DATA_DIR', '/data')) pattern.
3. Never assume /home/, /Users/, or C:\\ paths exist in containers."""
    else:
        import platform
        context = f"""Local development environment: {platform.system()}
Use pathlib.Path.home() for user directories.
Use os.environ.get() for configuration overrides."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"You are a code assistant. {context}",
        messages=[{"role": "user", "content": task}]
    )
    return response.content[0].text.strip()


code = ask_container_paths("Write code to read an app config file and write logs")
print(code)
# → Uses Path(os.environ.get('CONFIG_DIR', '/config')) / 'app.yaml'

# Expected Token Savings: correct container paths from the start; no env-mismatch debugging
# Environment: Dockerised agents, Kubernetes jobs, serverless functions
```

---

### Option 5 — Post-generation path normaliser

After code generation, scan for hardcoded paths and replace them with portable equivalents using AST analysis.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Mapping from hardcoded path patterns to portable replacements
PATH_REPLACEMENTS = [
    # Windows absolute paths
    (r'"C:\\\\Users\\\\[^"\\\\]+"', 'Path.home()'),
    (r"'C:\\\\Users\\\\[^'\\\\]+'", 'Path.home()'),
    # Linux home
    (r'"/home/\w+/', '(Path.home() / "'),
    (r"'/home/\w+/", "(Path.home() / '"),
    # macOS home
    (r'"/Users/\w+/', '(Path.home() / "'),
    # Config file extensions
    (r'Path\.home\(\)\s*/\s*"\.(\w+)/(\w+)\.json"',
     r'Path.home() / ".\1" / "\2.json"'),
]

PATH_IMPORT_NEEDED = "from pathlib import Path"


def normalise_paths(code: str) -> str:
    """Replace hardcoded paths with portable pathlib equivalents."""
    modified = code
    changes = 0

    for pattern, replacement in PATH_REPLACEMENTS:
        new_code = re.sub(pattern, replacement, modified)
        if new_code != modified:
            changes += 1
            modified = new_code

    # Ensure pathlib is imported if we made changes
    if changes > 0 and PATH_IMPORT_NEEDED not in modified:
        modified = PATH_IMPORT_NEEDED + "\n" + modified

    return modified


def generate_code(task: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": task}]
    )
    raw_code = response.content[0].text.strip()
    normalised = normalise_paths(raw_code)

    if normalised != raw_code:
        print("[normaliser] Replaced hardcoded paths with portable equivalents")

    return normalised


code = generate_code("Write code to read a config file from the user's home directory on Windows")
print(code)

# Expected Token Savings: post-processing eliminates debug round-trips for path errors
# Environment: legacy code generation agents where system prompt changes are impractical
```

---

### Option 6 — Schema-driven path generation with validation

Ask the model to return paths as structured JSON with type annotations, then validate and render them portably.

```python
import anthropic
import json
import os
import sys
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")


def resolve_path_spec(spec: dict) -> Path:
    """
    Convert a portable path spec into a real path for the current OS.
    spec: {"base": "home|config|data|temp|cwd", "parts": ["subdir", "file.json"]}
    """
    base_map = {
        "home":   Path.home(),
        "config": Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")),
        "data":   Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")),
        "temp":   Path(os.environ.get("TMPDIR", "/tmp")),
        "cwd":    Path.cwd(),
    }

    base = base_map.get(spec.get("base", "home"), Path.home())
    parts = spec.get("parts", [])
    return base.joinpath(*parts)


def ask_path_spec(task: str) -> dict[str, Path]:
    """Ask model for portable path specs; resolve them locally."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="""Return a JSON object mapping path names to portable path specs.
Each spec: {"base": "home|config|data|temp|cwd", "parts": ["subdir", "filename"]}
Do NOT use absolute paths. Use only the base keys listed above.
Example: {"config_file": {"base": "config", "parts": ["myapp", "settings.json"]}}""",
        messages=[{"role": "user", "content": f"Task: {task}\nReturn path specs JSON:"}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:-1])

    specs = json.loads(raw)
    return {name: resolve_path_spec(spec) for name, spec in specs.items()}


# Ask for paths — model returns portable specs, we resolve for current OS
paths = ask_path_spec("Config file, log directory, and temp cache for a CLI app called myapp")
for name, path in paths.items():
    print(f"{name}: {path}")
    # → config_file: /home/alice/.config/myapp/settings.json  (Linux)
    # → config_file: /Users/alice/.config/myapp/settings.json (macOS)
    # → log_dir: /home/alice/.local/share/myapp/logs

# Expected Token Savings: model never hallucinates paths; all paths correct for runtime OS
# Environment: CLI tools, developer agents generating config for end-user systems
```

---

## Comparison

| Option | Correct for OS | Cross-Platform | Validates Output | Container-Aware | Complexity |
|--------|---------------|----------------|------------------|-----------------|------------|
| 1 | Yes (runtime inject) | Yes | No | No | Low |
| 2 | Yes | Yes | Yes (regex) | No | Low |
| 3 | Yes (platformdirs) | Yes | No | No | Low |
| 4 | Yes (env vars) | Yes | No | Yes | Low |
| 5 | Partial (post-fix) | Yes | No | No | Low |
| 6 | Yes (spec schema) | Yes | Yes | No | Medium |

**Recommended starting point:** Option 1 — inject `os.path.expanduser('~')`, `platform.system()`, and `os.sep` into the system prompt before any path-related code generation. Add Option 2's regex validator as a post-generation gate to catch any hardcoded paths that slip through.
