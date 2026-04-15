---
layout: solution
title: "Agent Hallucinates File Paths in Generated Code"
category: hallucination
description: "Agent generates code with hardcoded file paths that don't exist in the actual filesystem — causing FileNotFoundError at runtime and requiring manual path correction."
tags: [hallucination, file-paths, code-generation, filesystem, runtime-errors]
---

## Symptom

Agent generates code with invented file paths that fail at runtime:

```python
# Agent generates this:
import json
config = json.load(open("/etc/myapp/config.json"))          # doesn't exist
log_dir = "/var/log/myapp/agent.log"                       # no such directory
data = open("/home/ubuntu/data/training_set.csv")          # wrong home dir
model_path = "/opt/models/llama-3.1-8b/model.bin"         # invented path
template = open("templates/email/welcome.html")             # relative path, wrong CWD

# All raise FileNotFoundError when run, even though the code logic is correct
# Developer must manually locate and fix each path before running
```

The agent uses plausible-sounding paths based on common conventions (Unix FHS, Python project layouts, Docker conventions) but has no knowledge of the actual filesystem structure.

## Root Cause

LLMs are trained on codebases from many different environments. When generating paths, the model interpolates from patterns it has seen — `/etc/`, `/opt/`, `~/.config/`, `./config/` — but has no grounding in the actual directory structure of the target environment. The hallucinated paths are internally consistent and look correct but are specific to an imagined environment that doesn't match reality.

## Fix

---

### Option 1: Inject Real Filesystem Context Before Code Generation

Before generating code, discover and inject the actual project structure so the model uses real paths.

```python
import os
import anthropic
from pathlib import Path

def scan_project_structure(root: str = ".", max_depth: int = 3) -> str:
    """Walk the project tree and return a compact representation."""
    root_path = Path(root).resolve()
    lines = [f"Project root: {root_path}"]

    # Find key files/directories
    for dirpath, dirnames, filenames in os.walk(root_path):
        depth = len(Path(dirpath).relative_to(root_path).parts)
        if depth > max_depth:
            dirnames.clear()
            continue
        # Skip hidden and common non-essential dirs
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git")]
        indent = "  " * depth
        rel = Path(dirpath).relative_to(root_path)
        lines.append(f"{indent}{rel}/")
        for fname in sorted(filenames)[:10]:  # Cap files per dir to avoid bloat
            lines.append(f"{indent}  {fname}")

    return "\n".join(lines[:100])  # Cap total output

def inject_env_paths() -> str:
    """Inject real environment-specific paths."""
    home = Path.home()
    cwd = Path.cwd()
    return (
        f"Runtime environment:\n"
        f"  CWD: {cwd}\n"
        f"  HOME: {home}\n"
        f"  Python: {os.sys.executable}\n"
        f"  Platform: {os.sys.platform}\n"
    )

def generate_code_with_real_paths(task: str) -> str:
    project_tree = scan_project_structure(max_depth=2)
    env_info = inject_env_paths()

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Generate Python code for the task.\n\n"
            f"{env_info}\n"
            f"Project structure:\n{project_tree}\n\n"
            "Rules for file paths:\n"
            "- Use ONLY paths that exist in the project structure above\n"
            "- For config files: prefer environment variables (os.environ.get('CONFIG_PATH'))\n"
            "- For relative paths: they are relative to CWD shown above\n"
            "- If a path is uncertain, use os.path.join(Path(__file__).parent, 'filename')\n"
            "- Never invent paths like /opt/models/ or /etc/myapp/ unless shown above"
        ),
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

code = generate_code_with_real_paths(
    "Write a script to load the project config and log to the project's log directory"
)
print(code)
```

**Expected Token Savings:** Real path injection adds ~200 tokens but prevents 2-4 correction turns (each ~400 tokens) for path-related errors. Net: saves 600-1,400 tokens per code generation session that would have had path errors.
**Environment:** Run `scan_project_structure()` from the project root. Cap depth at 2-3 to avoid token bloat for large monorepos. Refresh the scan when file structure changes significantly.

---

### Option 2: Path Existence Validator — Test All Paths Before Returning Code

After generating code, extract all file path strings and verify they exist before returning the code to the user.

```python
import ast
import os
import re
import anthropic

def extract_string_paths(code: str) -> list[str]:
    """Extract string literals that look like file paths from Python code."""
    path_candidates = []

    # Regex for string literals that look like paths
    string_patterns = [
        r'["\'](/[\w./\-_]+)["\']',          # absolute Unix paths
        r'["\']([A-Z]:\\[\w\\\-_.]+)["\']',  # Windows absolute paths
        r'open\(["\']([^"\']+\.(?:json|csv|txt|yaml|yml|cfg|conf|log|pkl|bin|h5))["\']',
        r'Path\(["\']([^"\']+)["\']',
        r'os\.path\.join\([^)]+\)',
    ]

    for pattern in string_patterns:
        matches = re.findall(pattern, code)
        path_candidates.extend(matches)

    # Also parse AST for string literals near file operations
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if ("/" in val or "\\" in val) and len(val) > 3:
                    path_candidates.append(val)
    except SyntaxError:
        pass

    # Filter: must look like a path (contains / or \)
    return list(set(p for p in path_candidates if "/" in p or "\\" in p))

def validate_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Returns (existing_paths, missing_paths)."""
    existing, missing = [], []
    for path in paths:
        # Expand ~ and env vars
        expanded = os.path.expandvars(os.path.expanduser(path))
        if os.path.exists(expanded):
            existing.append(path)
        else:
            # Check if parent exists (path may be a new file)
            parent = os.path.dirname(expanded)
            if parent and os.path.exists(parent):
                existing.append(path)  # Parent exists — path is a new file, OK
            else:
                missing.append(path)
    return existing, missing

def generate_and_validate_paths(task: str, max_retries: int = 2) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    for attempt in range(max_retries):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="Generate Python code for the task. Use relative paths or environment variables where possible.",
            messages=messages,
        )
        code = response.content[0].text
        messages.append({"role": "assistant", "content": code})

        # Validate paths in generated code
        paths = extract_string_paths(code)
        existing, missing = validate_paths(paths)

        if not missing:
            print(f"Path validation passed ({len(existing)} paths OK)")
            return code

        # Fix: inject real filesystem info and ask for correction
        print(f"Path validation failed: {missing}")
        cwd = os.getcwd()
        real_paths = [str(p) for p in [
            *os.listdir("."),
            *([str(f) for f in os.listdir("config")] if os.path.exists("config") else []),
        ]]

        messages.append({
            "role": "user",
            "content": (
                f"These paths don't exist: {missing}\n"
                f"Current directory: {cwd}\n"
                f"Files available: {real_paths[:20]}\n"
                "Fix the code to use paths that actually exist, or use os.environ.get() for configurable paths."
            ),
        })

    return response.content[0].text

code = generate_and_validate_paths("Write a script to read config.json and write output to logs/output.txt")
print(code)
```

**Expected Token Savings:** Path validation catches errors before the user runs the code. Each caught error prevents a manual debug cycle (typically 2-3 correction turns × 400 tokens = 800-1,200 tokens). Validation adds ~50 tokens of logic but saves multiple turns.
**Environment:** `os.path.exists()` only works for the local filesystem. For Docker/container paths, validate inside a test container. For remote paths (S3, GCS), add protocol-specific validators.

---

### Option 3: Environment Variable Pattern — Generate Path-Agnostic Code

Instruct the agent to generate code that reads paths from environment variables instead of hardcoding them, making paths configurable at runtime.

```python
import anthropic

ENV_VAR_SYSTEM = """Generate Python code that uses environment variables for all file paths.

Rules:
1. NEVER hardcode absolute paths like /etc/app/config.json or /opt/models/
2. For config files: CONFIG_PATH = os.environ.get("CONFIG_PATH", "config/settings.json")
3. For data directories: DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
4. For log files: LOG_FILE = os.environ.get("LOG_FILE", "./logs/app.log")
5. For model files: MODEL_PATH = os.environ.get("MODEL_PATH") — no default if required
6. Always use Path(__file__).parent for paths relative to the script itself
7. Use Path.home() / ".config" / "appname" for user config directories
8. Create directories if they don't exist: path.mkdir(parents=True, exist_ok=True)

Template pattern:
```python
import os
from pathlib import Path

# All paths configurable via environment
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", Path(__file__).parent / "config.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).parent / "logs"))

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
```"""

def generate_portable_code(task: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ENV_VAR_SYSTEM,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Test cases that previously produced hardcoded paths
tasks = [
    "Write a script that loads a YAML config file and processes CSV data files",
    "Create a logging setup that writes to a rotating log file",
    "Build a script that loads a trained ML model and runs inference",
]

for task in tasks:
    print(f"Task: {task!r}")
    code = generate_portable_code(task)
    # Verify no hardcoded absolute paths
    import re
    hardcoded = re.findall(r'["\']/(etc|opt|home|var|usr)/[^"\']*["\']', code)
    if hardcoded:
        print(f"WARNING: Found potentially hardcoded paths: {hardcoded}")
    else:
        print("Path pattern: PORTABLE (uses env vars)")
    print(code[:200])
    print()
```

**Expected Token Savings:** Portable code works in any environment without modification. Eliminates all path-related FileNotFoundError correction turns. System prompt adds ~300 tokens once; saves 2-4 correction turns × 400 tokens = 800-1,600 tokens per session with path issues.
**Environment:** Include a `.env.example` file in the generated code listing all path-related env vars. Works across Docker, Kubernetes, local dev, and CI without code changes — just different env var values.

---

### Option 4: Path Discovery Tool — Let the Agent Find Real Paths Before Generating Code

Give the agent a `find_file` tool so it can discover actual paths rather than inventing them.

```python
import os
import fnmatch
import anthropic

client = anthropic.Anthropic()

def find_file(pattern: str, search_root: str = ".") -> list[str]:
    """Find files matching a pattern. Returns list of matching paths."""
    matches = []
    root = os.path.abspath(search_root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            if fnmatch.fnmatch(fname, pattern):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root)
                matches.append(rel)
        if len(matches) > 20:  # Cap to prevent huge results
            break
    return matches

def list_directory(path: str = ".") -> list[str]:
    """List contents of a directory."""
    try:
        entries = os.listdir(path)
        return sorted(entries)[:30]
    except FileNotFoundError:
        return [f"ERROR: {path} does not exist"]

tools = [
    {
        "name": "find_file",
        "description": "Find files matching a glob pattern in the project",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.json' or 'config*'"},
                "search_root": {"type": "string", "description": "Directory to search (default: current dir)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the contents of a directory",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path"}},
            "required": [],
        },
    },
]

def dispatch_tool(name: str, args: dict) -> str:
    if name == "find_file":
        results = find_file(args.get("pattern", "*"), args.get("search_root", "."))
        return f"Found: {results}" if results else "No files found matching that pattern"
    elif name == "list_directory":
        results = list_directory(args.get("path", "."))
        return f"Contents: {results}"
    return f"Unknown tool: {name}"

def generate_code_with_discovery(task: str) -> str:
    messages = [
        {
            "role": "user",
            "content": (
                f"{task}\n\n"
                "Before writing code, use find_file and list_directory to discover "
                "the actual file structure. Use only paths you confirm exist."
            ),
        }
    ]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="Generate Python code. Always discover real paths before using them.",
            tools=tools,
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": tu.id,
             "content": dispatch_tool(tu.name, tu.input)}
            for tu in tool_uses
        ]
        messages.append({"role": "user", "content": results})

    return "Max turns"

code = generate_code_with_discovery(
    "Write a Python script that reads the project's configuration file and processes any CSV data files"
)
print(code)
```

**Expected Token Savings:** Path discovery uses 1-2 extra tool turns (~800 tokens) but eliminates all hallucinated path errors. Each prevented error saves 2-4 correction turns × 400 tokens = 800-1,600 tokens. Break-even after 1 prevented error.
**Environment:** `find_file` searches only the local filesystem. For S3/GCS paths, add a `list_bucket` tool. Cap search results to prevent the agent from enumerating huge directories.

---

### Option 5: pathlib.Path Relative-to-Script Pattern — Generate Self-Locating Code

Teach the agent to use `Path(__file__).parent` as the anchor for all relative paths, making generated code portable regardless of the caller's CWD.

```python
import anthropic

PATHLIB_SYSTEM = """Generate Python code using pathlib.Path for all file operations.

Pattern: anchor all paths to the script's own location using Path(__file__).parent
This makes code work regardless of the caller's working directory.

Examples:
```python
from pathlib import Path

# Script-relative paths (CORRECT — works from any CWD)
SCRIPT_DIR = Path(__file__).parent
CONFIG = SCRIPT_DIR / "config.json"
DATA_DIR = SCRIPT_DIR / "data"
LOG_DIR = SCRIPT_DIR / "logs"
OUTPUT = SCRIPT_DIR / "output" / "results.csv"

# Reading config
with open(CONFIG) as f:
    config = json.load(f)

# Creating output directories
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Iterating files
for csv_file in DATA_DIR.glob("*.csv"):
    process(csv_file)
```

Do NOT use:
- Hardcoded absolute paths: /etc/app/config.json
- CWD-relative paths without anchor: open("config.json")  ← breaks if CWD changes
- os.path.join strings: use / operator instead
"""

def generate_pathlib_code(task: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=PATHLIB_SYSTEM,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Check generated code for anti-patterns
def check_path_patterns(code: str) -> dict:
    import re
    issues = []
    # Hardcoded absolute paths
    for m in re.finditer(r'["\']/(etc|opt|home|var|usr|tmp)/[^"\']*["\']', code):
        issues.append(f"Hardcoded absolute path: {m.group(0)}")
    # CWD-relative without anchor
    for m in re.finditer(r'open\(["\'](?!/)(?![A-Z]:)([^"\']+\.(?:json|csv|txt|yaml))["\']', code):
        if "__file__" not in code[max(0, m.start()-200):m.start()]:
            issues.append(f"Unanchored relative path: {m.group(0)}")

    return {
        "issues": issues,
        "uses_pathlib": "pathlib" in code or "Path(" in code,
        "uses_file_anchor": "__file__" in code,
        "ok": len(issues) == 0,
    }

tasks = [
    "Load a JSON config file, process CSV files from a data directory, write results to output/",
    "Set up logging to a file in the logs directory",
    "Load a pickle model file from the models directory",
]

for task in tasks:
    code = generate_pathlib_code(task)
    check = check_path_patterns(code)
    print(f"Task: {task[:60]!r}")
    print(f"  OK: {check['ok']}, uses pathlib: {check['uses_pathlib']}, anchored: {check['uses_file_anchor']}")
    if check["issues"]:
        print(f"  Issues: {check['issues']}")
    print()
```

**Expected Token Savings:** System prompt adds ~300 tokens but generates code that works in any environment without path corrections. Zero follow-up correction turns for FileNotFoundError. For a 10-session project: saves 10 × 2 correction turns × 400 tokens = 8,000 tokens total.
**Environment:** `Path(__file__).parent` works for scripts run directly. For installed packages, use `importlib.resources` instead. This pattern is especially valuable for scripts distributed to other developers who will run them from different working directories.

---

### Option 6: Static Analysis Post-Processing — Scan and Flag Suspicious Paths

After generation, run a static analysis pass to flag all file paths, classify them as safe/risky, and annotate the code with warnings.

```python
import re
import anthropic

SAFE_PATH_PATTERNS = [
    r"Path\(__file__\)",
    r"os\.environ",
    r"Path\.home\(\)",
    r"tempfile\.",
    r"os\.path\.expanduser",
    r"os\.path\.expandvars",
]

RISKY_PATH_PATTERNS = [
    (r'["\']/(etc|opt|var/log|home/\w+|usr/local)[^"\']*["\']',
     "Hardcoded Unix system path — likely won't exist on target system"),
    (r'["\']C:\\[^"\']+["\']',
     "Hardcoded Windows path — not portable"),
    (r'open\(["\'](?!http)([^"\']{3,})["\']',
     "Unanchored file open — will fail if CWD differs from expectation"),
    (r'["\']~/', "Tilde path — may not expand correctly; use Path.home() instead"),
]

def analyse_paths(code: str) -> dict:
    risks = []
    safe_indicators = []

    for pattern in SAFE_PATH_PATTERNS:
        if re.search(pattern, code):
            safe_indicators.append(pattern)

    for pattern, description in RISKY_PATH_PATTERNS:
        for match in re.finditer(pattern, code):
            line_num = code[:match.start()].count("\n") + 1
            risks.append({"line": line_num, "match": match.group(0)[:60], "risk": description})

    return {
        "risks": risks,
        "safe_indicators": safe_indicators,
        "score": "SAFE" if not risks else ("WARNING" if len(risks) < 3 else "RISKY"),
    }

def annotate_code(code: str, analysis: dict) -> str:
    """Prepend risk warnings to the code as comments."""
    if not analysis["risks"]:
        return code
    warning_lines = [
        "# PATH ANALYSIS WARNINGS (review before running):",
        *[f"# Line {r['line']}: {r['risk']}" for r in analysis["risks"]],
        "# ---",
        "",
    ]
    return "\n".join(warning_lines) + code

# Comparison table
"""
| Approach | Prevention | Portability | Tool Calls | Complexity |
|---|---|---|---|---|
| Option 1: Inject real FS context | High | Medium | No | Low |
| Option 2: Path existence validator | High | No | No | Medium |
| Option 3: Env var pattern | Very High | Very High | No | Low |
| Option 4: Path discovery tools | Very High | High | Yes | Medium |
| Option 5: pathlib anchor pattern | High | Very High | No | Low |
| Option 6: Static analysis scan | Medium (post-gen) | No | No | Low |
"""

def generate_and_analyse(task: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Generate Python code for the task. Prefer environment variables and pathlib for file paths.",
        messages=[{"role": "user", "content": task}],
    )
    code = response.content[0].text
    analysis = analyse_paths(code)
    print(f"Path analysis: {analysis['score']}, {len(analysis['risks'])} risks")
    for risk in analysis["risks"]:
        print(f"  Line {risk['line']}: {risk['risk']}")
    return annotate_code(code, analysis)

result = generate_and_analyse(
    "Write a script that reads from /etc/app/config.json and logs to /var/log/myapp/"
)
print(result[:400])
```

**Expected Token Savings:** Static analysis costs zero extra LLM calls. Annotated warnings help developers fix paths before running, preventing runtime error correction cycles (2-3 turns × 400 tokens = 800-1,200 tokens per error). For automated pipelines, use the `score` field to reject risky code automatically.
**Environment:** Pattern list must be maintained as new risky patterns emerge. Combine with Option 3 (env vars) to prevent issues rather than just detecting them. In CI/CD: fail the pipeline if `score == "RISKY"` and require human review.
