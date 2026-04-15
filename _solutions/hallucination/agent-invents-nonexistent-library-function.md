---
layout: solution
title: "Agent Invents Nonexistent Library Function — AttributeError at Runtime"
category: hallucination
description: "Agent writes code calling a function that doesn't exist in the library. Code looks plausible. Fails with AttributeError or ImportError at runtime. Agent used a function it hallucinated from training data."
tags: [hallucination, attributeerror, library, function, api, runtime-error, fabricated]
---

# Agent Invents Nonexistent Library Function — AttributeError at Runtime

## Symptom
- `AttributeError: module 'pandas' has no attribute 'read_excel_cached'`
- `AttributeError: 'OpenAI' object has no attribute 'complete'` (correct method is `chat.completions.create`)
- Code passes a code review but fails immediately at runtime
- Agent confidently uses `.upsert_many()` which doesn't exist in the ORM
- Agent mixes function signatures from two different library versions

## Root Cause
LLM training data includes library docs, Stack Overflow, and GitHub code from multiple versions and forks. The model synthesizes plausible-sounding function names that don't actually exist, or uses outdated APIs from an older version. This is especially common for: infrequently used modules, recently released libraries, and functions that sound like they should exist but don't.

## Fix

### Option 1: Verify function existence before using it

```python
import inspect

def verify_api_exists(obj, method_name: str, check_callable: bool = True) -> bool:
    """
    Check that a method/attribute exists on an object before calling it.
    Use this when an agent generates code using library APIs.
    """
    if not hasattr(obj, method_name):
        available = [m for m in dir(obj) if not m.startswith("_")]
        # Find closest match
        import difflib
        closest = difflib.get_close_matches(method_name, available, n=3, cutoff=0.6)
        raise AttributeError(
            f"'{type(obj).__name__}' has no attribute '{method_name}'.\n"
            f"Did you mean: {closest}\n"
            f"Available methods: {available[:20]}"
        )

    attr = getattr(obj, method_name)
    if check_callable and not callable(attr):
        raise TypeError(f"'{method_name}' exists but is not callable — it's a {type(attr).__name__}")

    return True

# Usage before executing agent-generated code:
import pandas as pd
verify_api_exists(pd, "read_excel")       # OK
verify_api_exists(pd, "read_excel_cached") # Raises with suggestions
```

### Option 2: Use introspection to get real API surface

```python
import importlib
import inspect

def get_real_api_surface(module_name: str, class_name: str = None) -> dict:
    """
    Get the actual public API of a module/class.
    Feed this into the agent prompt so it uses real function names.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        return {"error": f"Module not installed: {e}"}

    target = module
    if class_name:
        target = getattr(module, class_name, None)
        if target is None:
            return {"error": f"No class '{class_name}' in {module_name}"}

    methods = {}
    for name in dir(target):
        if name.startswith("_"):
            continue
        attr = getattr(target, name)
        if callable(attr):
            try:
                sig = str(inspect.signature(attr))
                doc = (inspect.getdoc(attr) or "")[:100]
                methods[name] = {"signature": sig, "doc": doc}
            except (ValueError, TypeError):
                methods[name] = {"signature": "(...)", "doc": ""}

    return {
        "module": module_name,
        "class": class_name,
        "version": getattr(module, "__version__", "unknown"),
        "methods": methods
    }

# Include in agent system prompt:
api = get_real_api_surface("anthropic", "Anthropic")
# → {methods: {complete: ..., messages: ...}} — agent sees actual methods
```

### Option 3: Sandbox test before returning generated code

```python
import subprocess
import tempfile
import textwrap

def test_generated_code(code: str, timeout: int = 10) -> dict:
    """
    Run generated code in a subprocess to catch AttributeError before delivery.
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    # Wrap in try/except to capture runtime errors
    test_code = textwrap.dedent(f"""
        import sys
        try:
            {textwrap.indent(code, '    ')}
            print("OK")
        except AttributeError as e:
            print(f"ATTRIBUTE_ERROR: {{e}}", file=sys.stderr)
            sys.exit(1)
        except ImportError as e:
            print(f"IMPORT_ERROR: {{e}}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"RUNTIME_ERROR: {{type(e).__name__}}: {{e}}", file=sys.stderr)
            sys.exit(1)
    """)

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(test_code)
        tmp_path = f.name

    result = subprocess.run(
        ["python", tmp_path],
        capture_output=True,
        text=True,
        timeout=timeout
    )

    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True}

# After agent generates code:
check = test_generated_code("import pandas as pd\ndf = pd.read_excel_cached('test.xlsx')")
if not check["ok"]:
    print(f"Agent hallucinated API: {check['error']}")
    # Re-prompt agent with error
```

### Option 4: System prompt with version-pinned API references

```python
def build_library_context(libraries: list[tuple[str, str]]) -> str:
    """
    Build a system prompt section with the exact installed versions.
    Prevents agent from using API from wrong version.
    """
    lines = ["Exact library versions installed in this environment:"]
    for pkg, version in libraries:
        lines.append(f"  - {pkg}=={version}")
    lines.append("")
    lines.append("Rules:")
    lines.append("- ONLY use functions that exist in these exact versions")
    lines.append("- Do NOT use functions from older or newer versions")
    lines.append("- If unsure whether a function exists, say so rather than guessing")
    lines.append("- Prefer official documentation examples over memory")
    return "\n".join(lines)

import pkg_resources

def get_installed_versions(packages: list[str]) -> list[tuple[str, str]]:
    result = []
    for pkg in packages:
        try:
            version = pkg_resources.get_distribution(pkg).version
            result.append((pkg, version))
        except pkg_resources.DistributionNotFound:
            result.append((pkg, "NOT INSTALLED"))
    return result

versions = get_installed_versions(["pandas", "anthropic", "httpx", "sqlalchemy"])
context = build_library_context(versions)
# → "pandas==2.2.1\nanthropic==0.40.0\n..."
```

### Option 5: Lint agent output with AST before execution

```python
import ast
import importlib

class HallucinatedAPIDetector(ast.NodeVisitor):
    """
    Walk the AST of agent-generated code and check that called
    attributes actually exist on the module being used.
    """

    def __init__(self):
        self.issues = []
        self._imports = {}  # alias -> module_name

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name
            self._imports[name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            name = alias.asname or alias.name
            self._imports[name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id in self._imports:
            module_name = self._imports[node.value.id]
            try:
                mod = importlib.import_module(module_name)
                if not hasattr(mod, node.attr):
                    self.issues.append(
                        f"Line {node.lineno}: '{module_name}' has no attribute '{node.attr}'"
                    )
            except ImportError:
                pass  # Can't check uninstalled modules
        self.generic_visit(node)

def detect_hallucinated_apis(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
        detector = HallucinatedAPIDetector()
        detector.visit(tree)
        return detector.issues
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

# Example:
issues = detect_hallucinated_apis("""
import pandas as pd
df = pd.read_excel_cached('file.xlsx')
df2 = pd.DataFrame.from_records_lazy(data)
""")
# → ["Line 2: 'pandas' has no attribute 'read_excel_cached'",
#    "Line 3: 'pandas' has no attribute 'from_records_lazy'"]
```

### Option 6: Correct common hallucinations with a known-issues map

```python
# Maintain a map of commonly hallucinated functions and their real equivalents
HALLUCINATED_TO_REAL = {
    # Anthropic SDK
    ("anthropic", "complete"): "client.messages.create(model=..., messages=[...])",
    ("anthropic", "chat"): "client.messages.create(model=..., messages=[...])",

    # Pandas
    ("pandas", "read_excel_cached"): "pd.read_excel()",
    ("pandas", "DataFrame.upsert"): "Use pd.concat() then drop_duplicates()",
    ("pandas", "merge_many"): "Use functools.reduce with pd.merge()",

    # SQLAlchemy
    ("sqlalchemy", "upsert_many"): "session.bulk_insert_mappings() or PostgreSQL ON CONFLICT",
    ("sqlalchemy", "Session.upsert"): "Use merge() for upsert semantics",

    # Httpx
    ("httpx", "get_json"): "client.get(url).json()",
    ("httpx", "Client.fetch"): "client.get() or client.request()",
}

def check_hallucination(module: str, attr: str) -> str | None:
    """Return correction hint if this is a known hallucination"""
    key = (module.split(".")[0], attr)
    return HALLUCINATED_TO_REAL.get(key)

# When agent uses a known hallucinated API:
hint = check_hallucination("anthropic", "complete")
if hint:
    print(f"Hallucinated API detected. Real equivalent: {hint}")
```

## Common Hallucinated APIs by Library

| Library | Hallucinated | Real |
|---|---|---|
| `anthropic` | `.complete()` | `.messages.create()` |
| `pandas` | `pd.read_excel_cached()` | `pd.read_excel()` |
| `openai` | `.ChatCompletion.create()` (v0) | `.chat.completions.create()` (v1) |
| `sqlalchemy` | `session.upsert()` | `session.merge()` |
| `httpx` | `.get_json()` | `.get().json()` |
| `boto3` | `s3.upload()` | `s3.upload_file()` |

## Expected Token Savings
Debugging hallucinated API + fix cycle: ~8,000 tokens
API verification before execution catches it instantly: ~200 tokens

## Environment
- Any agent that generates code using external libraries
- Source: direct experience; API hallucination is most common for SDK clients and data libraries
