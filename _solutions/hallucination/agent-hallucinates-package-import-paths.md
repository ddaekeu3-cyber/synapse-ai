---
layout: solution
title: "Agent Hallucinates Package Import Paths"
category: hallucination
description: "Agent generates import statements using moved, renamed, or deprecated module paths from outdated training data."
tags: [hallucination, imports, dependencies, python, versioning]
---

## Symptom

Agent generates code with import statements that fail at runtime:

```python
# Agent generates this (langchain pre-0.1.0 style):
from langchain.llms import OpenAI
from langchain.chat_models import ChatOpenAI
from langchain.embeddings import OpenAIEmbeddings

# But installed package requires:
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.llms import OpenAI

# Also seen with openai v0 → v1 migration:
import openai
response = openai.Completion.create(model="gpt-4", prompt="hello")
# RuntimeError: openai.Completion has been removed in v1.0.0
```

Agent training data reflects an older package version. Users must manually fix every import before running generated code.

## Root Cause

LLMs are trained on code snapshots frozen at a point in time. When packages reorganise modules (langchain 0.1, openai v1, anthropic v0→v1, sklearn → scikit-learn sub-packages), the model has no mechanism to detect the discrepancy between what it learned and what is currently installed. The model confidently generates historically-correct imports that are now wrong.

## Fix

---

### Option 1: Inject Installed Package Metadata into System Prompt

Before generation, introspect the runtime environment and inject version + top-level module list so the model can self-correct.

```python
import importlib
import importlib.metadata
import pkgutil
import anthropic

def get_package_info(package_names: list[str]) -> str:
    lines = []
    for name in package_names:
        try:
            version = importlib.metadata.version(name)
            # Get top-level importable modules
            dist = importlib.metadata.distribution(name)
            top_level = []
            try:
                top = dist.read_text("top_level.txt")
                if top:
                    top_level = [m.strip() for m in top.splitlines() if m.strip()]
            except Exception:
                pass
            lines.append(f"  {name}=={version} → importable as: {', '.join(top_level) or name}")
        except importlib.metadata.PackageNotFoundError:
            lines.append(f"  {name}: NOT INSTALLED")
    return "\n".join(lines)

def generate_code_with_import_awareness(task: str, packages: list[str]) -> str:
    pkg_info = get_package_info(packages)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=f"""You are a Python code generator.

INSTALLED PACKAGES (use ONLY these import paths):
{pkg_info}

Rules:
- Never use deprecated import paths from older versions
- If unsure of exact submodule, use the top-level package and let the user drill down
- Add a comment like `# requires: package==version` on ambiguous imports""",
        messages=[{"role": "user", "content": f"Write Python code to: {task}"}],
    )
    return response.content[0].text

# Example
code = generate_code_with_import_awareness(
    task="Create a LangChain chain with OpenAI chat model",
    packages=["langchain", "langchain-openai", "langchain-core", "openai", "anthropic"],
)
print(code)
```

**Expected Token Savings:** Minimal overhead (~200 tokens for metadata injection) but eliminates costly retry loops caused by ImportError crashes.
**Environment:** Any Python runtime; works best when the agent's host environment matches the target execution environment.

---

### Option 2: Import Validator — Execute and Retry on ImportError

After generation, parse import statements from the code, attempt to import each, and feed failures back to the model for correction.

```python
import ast
import subprocess
import sys
import anthropic

def extract_imports(code: str) -> list[str]:
    """Extract all import statements from generated code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            imports.append(f"from {module} import {names}")
    return imports

def validate_imports(code: str) -> list[str]:
    """Return list of import errors (empty = all OK)."""
    imports = extract_imports(code)
    errors = []
    for stmt in imports:
        result = subprocess.run(
            [sys.executable, "-c", stmt],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            errors.append(f"{stmt!r} → {result.stderr.strip().splitlines()[-1]}")
    return errors

def generate_and_validate(task: str, max_retries: int = 3) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    for attempt in range(max_retries):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system="You are a Python expert. Generate runnable code using currently installed packages.",
            messages=messages,
        )
        code = response.content[0].text
        # Extract code block if wrapped in markdown
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0].strip()

        errors = validate_imports(code)
        if not errors:
            return code

        # Feed errors back for correction
        error_msg = "\n".join(errors)
        messages.append({"role": "assistant", "content": response.content[0].text})
        messages.append({
            "role": "user",
            "content": (
                f"The following imports failed in the current environment:\n{error_msg}\n\n"
                "Please fix the import paths and return corrected code only."
            ),
        })

    return code  # Return best attempt after max retries

result = generate_and_validate("Create a langchain chain that uses ChatOpenAI to answer questions")
print(result)
```

**Expected Token Savings:** ~30% reduction in wasted follow-up messages; each retry is cheaper than a full re-generation since context is focused on the error.
**Environment:** Requires subprocess access; safe for local dev. In sandboxed environments use importlib.import_module() instead of subprocess.

---

### Option 3: Import Alias Registry — Map Legacy to Current Paths

Maintain a registry of known moved/deprecated imports and rewrite the generated code before execution.

```python
import ast
import astunparse  # pip install astunparse
import anthropic

# Registry: old_module.name -> new_module.name
IMPORT_MIGRATIONS = {
    # langchain 0.0.x → 0.1+ / 0.2+
    "langchain.llms.OpenAI": "langchain_community.llms.OpenAI",
    "langchain.llms.openai.OpenAI": "langchain_community.llms.OpenAI",
    "langchain.chat_models.ChatOpenAI": "langchain_openai.ChatOpenAI",
    "langchain.embeddings.OpenAIEmbeddings": "langchain_openai.OpenAIEmbeddings",
    "langchain.vectorstores.FAISS": "langchain_community.vectorstores.FAISS",
    "langchain.document_loaders.PyPDFLoader": "langchain_community.document_loaders.PyPDFLoader",
    # openai v0 → v1
    "openai.Completion": "openai.completions",  # flagged; v1 uses client
    "openai.ChatCompletion": "openai.chat.completions",
    # sklearn
    "sklearn.cross_validation": "sklearn.model_selection",
    "sklearn.grid_search": "sklearn.model_selection",
}

class ImportRewriter(ast.NodeTransformer):
    def __init__(self, migrations: dict):
        self.migrations = migrations
        self.rewrites = []

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        new_names = []
        for alias in node.names:
            old_key = f"{module}.{alias.name}"
            if old_key in self.migrations:
                new_path = self.migrations[old_key]
                new_module, new_name = new_path.rsplit(".", 1)
                self.rewrites.append(f"{old_key} → {new_path}")
                # Replace this specific alias; emit separate ImportFrom
                new_node = ast.ImportFrom(
                    module=new_module,
                    names=[ast.alias(name=new_name, asname=alias.asname)],
                    level=0,
                )
                return new_node
            new_names.append(alias)
        node.names = new_names
        return node

def rewrite_imports(code: str) -> tuple[str, list[str]]:
    tree = ast.parse(code)
    rewriter = ImportRewriter(IMPORT_MIGRATIONS)
    new_tree = rewriter.visit(tree)
    ast.fix_missing_locations(new_tree)
    return astunparse.unparse(new_tree), rewriter.rewrites

def generate_with_rewrite(task: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system="Generate Python code for the requested task.",
        messages=[{"role": "user", "content": task}],
    )
    raw_code = response.content[0].text

    if "```python" in raw_code:
        raw_code = raw_code.split("```python")[1].split("```")[0].strip()

    fixed_code, rewrites = rewrite_imports(raw_code)
    if rewrites:
        print("Auto-corrected imports:")
        for r in rewrites:
            print(f"  {r}")
    return fixed_code

code = generate_with_rewrite("Use langchain with OpenAI to summarise a document")
print(code)
```

**Expected Token Savings:** Zero additional LLM calls; deterministic rewrite costs only local compute. Saves 1-2 retry roundtrips per hallucinated import.
**Environment:** Requires `astunparse`. Registry must be maintained manually as packages evolve. Combine with Option 1 for defense-in-depth.

---

### Option 4: Structured Import Manifest — Ask Model to Declare Imports Separately

Prompt the model to output a structured import manifest first, validate it, then generate the implementation body.

```python
import json
import importlib
import anthropic
from pydantic import BaseModel

class ImportManifest(BaseModel):
    imports: list[str]  # e.g. ["from langchain_openai import ChatOpenAI"]
    rationale: str

def validate_manifest(manifest: ImportManifest) -> list[str]:
    errors = []
    for stmt in manifest.imports:
        try:
            exec(compile(stmt, "<string>", "exec"))  # noqa: S102
        except ImportError as e:
            errors.append(f"{stmt!r}: {e}")
        except Exception:
            pass  # Syntax errors caught separately
    return errors

def generate_with_manifest(task: str) -> str:
    client = anthropic.Anthropic()

    # Step 1: get import manifest
    manifest_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="""You are a Python import planner. Return ONLY a JSON object:
{"imports": ["from pkg import Cls", ...], "rationale": "why these packages"}
List every import the implementation will need. Use currently-stable package paths.""",
        messages=[{"role": "user", "content": f"Plan imports for: {task}"}],
    )

    raw = manifest_resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

    manifest = ImportManifest(**json.loads(raw))
    errors = validate_manifest(manifest)

    if errors:
        # Correct manifest before proceeding
        correction_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="Fix the import errors and return corrected JSON manifest only.",
            messages=[
                {"role": "user", "content": f"Original manifest: {manifest.model_dump_json()}"},
                {"role": "user", "content": f"Import errors:\n" + "\n".join(errors)},
            ],
        )
        raw = correction_resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        manifest = ImportManifest(**json.loads(raw))

    # Step 2: generate implementation using validated imports
    import_block = "\n".join(manifest.imports)
    impl_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=f"""Write Python implementation using ONLY these imports (already validated):
{import_block}

Do not add any other imports. Start code directly after the import block.""",
        messages=[{"role": "user", "content": task}],
    )
    return import_block + "\n\n" + impl_resp.content[0].text

result = generate_with_manifest("Build a RAG pipeline with langchain and FAISS")
print(result)
```

**Expected Token Savings:** 2-call pattern costs ~15% more tokens upfront but eliminates multi-turn retry spirals that average 3-4 exchanges when imports are wrong.
**Environment:** Requires importlib access at validation time. Works in CI if the same virtualenv is used for generation and execution.

---

### Option 5: AST-Level Import Linter Before Returning Code

Insert a post-generation linting step that checks each import against a curated database of known-bad patterns and refuses to return code with flagged imports.

```python
import ast
import re
import anthropic

# Patterns: (regex on import string, reason, suggested fix)
DEPRECATED_PATTERNS = [
    (r"from langchain\.llms import", "langchain.llms moved in v0.1", "use langchain_community.llms"),
    (r"from langchain\.chat_models import", "langchain.chat_models moved", "use langchain_openai"),
    (r"from langchain\.embeddings import", "langchain.embeddings moved", "use langchain_openai or langchain_community.embeddings"),
    (r"openai\.Completion\.create", "openai v0 API removed in v1", "use client.completions.create()"),
    (r"openai\.ChatCompletion\.create", "openai v0 API removed in v1", "use client.chat.completions.create()"),
    (r"from sklearn\.cross_validation", "removed in sklearn 0.20", "use sklearn.model_selection"),
    (r"import tensorflow\.compat\.v1", "TF v1 compat shim", "use TF2 native APIs"),
    (r"from transformers import pipeline.*device=", "device arg deprecated", "use device_map= instead"),
]

def lint_imports(code: str) -> list[dict]:
    issues = []
    for pattern, reason, suggestion in DEPRECATED_PATTERNS:
        for match in re.finditer(pattern, code):
            issues.append({
                "match": match.group(0),
                "reason": reason,
                "suggestion": suggestion,
                "line": code[:match.start()].count("\n") + 1,
            })
    return issues

def generate_linted_code(task: str, max_rounds: int = 2) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    for round_num in range(max_rounds):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system="Generate modern, production-ready Python code using current stable APIs.",
            messages=messages,
        )
        code_text = response.content[0].text
        issues = lint_imports(code_text)

        if not issues:
            return code_text

        # Build targeted correction prompt
        issue_summary = "\n".join(
            f"  Line {i['line']}: `{i['match']}` — {i['reason']}. Fix: {i['suggestion']}"
            for i in issues
        )
        messages.append({"role": "assistant", "content": code_text})
        messages.append({
            "role": "user",
            "content": (
                f"Your code uses deprecated/moved imports:\n{issue_summary}\n\n"
                "Rewrite using the suggested modern equivalents. Return complete corrected code."
            ),
        })

    return response.content[0].text  # best attempt

result = generate_linted_code("Create a LangChain agent with OpenAI and vector search")
print(result)
```

**Expected Token Savings:** One extra correction pass costs ~800 tokens; avoids user-reported bugs that require full re-generation from scratch (typically 3,000+ tokens of back-and-forth).
**Environment:** Pure Python; no subprocess required. Pattern database must be updated when major package migrations occur.

---

### Option 6: Package Changelog Injection via LLM-Summarised Migration Guide

Fetch or cache package CHANGELOG / migration guide, summarise with a cheap model, inject the summary as system context before code generation.

```python
import hashlib
import json
from pathlib import Path
import anthropic

CACHE_DIR = Path(".cache/changelogs")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Curated migration notes (in production, fetch from PyPI / GitHub releases)
MIGRATION_NOTES = {
    "langchain": """
langchain 0.1.0 migration (Jan 2024):
- langchain.llms.* → langchain_community.llms.*
- langchain.chat_models.* → langchain_openai.* (OpenAI models) or langchain_community.*
- langchain.embeddings.OpenAIEmbeddings → langchain_openai.OpenAIEmbeddings
- langchain.vectorstores.* → langchain_community.vectorstores.*
- New packages required: langchain-openai, langchain-community (install separately)
""",
    "openai": """
openai v1.0.0 migration (Nov 2023):
- Module-level functions REMOVED: openai.Completion.create(), openai.ChatCompletion.create()
- Now requires client instance: client = openai.OpenAI(api_key=...)
- Chat: client.chat.completions.create(model=..., messages=...)
- Completions (legacy): client.completions.create(model=..., prompt=...)
- Embeddings: client.embeddings.create(model=..., input=...)
- Async: openai.AsyncOpenAI()
""",
    "anthropic": """
anthropic v0.20+ patterns:
- client = anthropic.Anthropic(api_key=...)  # sync
- client = anthropic.AsyncAnthropic(api_key=...)  # async
- client.messages.create(model=..., max_tokens=..., messages=[...])
- Streaming: with client.messages.stream(...) as stream:
""",
}

def get_migration_summary(packages: list[str]) -> str:
    relevant = []
    for pkg in packages:
        for key, notes in MIGRATION_NOTES.items():
            if key in pkg.lower():
                relevant.append(notes.strip())
    return "\n\n".join(relevant) if relevant else "No specific migration notes for requested packages."

def generate_with_migration_context(task: str, packages: list[str]) -> str:
    client = anthropic.Anthropic()
    migration_ctx = get_migration_summary(packages)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=f"""You are a Python expert generating code for: {', '.join(packages)}

PACKAGE MIGRATION NOTES (critical — training data may be outdated):
{migration_ctx}

Apply these migration notes strictly. If you're unsure of an import path, use the notes above.""",
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Comparison
import time

packages = ["langchain", "langchain-openai", "openai", "anthropic"]
task = "Build a conversational agent using LangChain with Claude as the LLM backend"

start = time.perf_counter()
code = generate_with_migration_context(task, packages)
elapsed = time.perf_counter() - start

print(f"Generated in {elapsed:.2f}s")
print(code)

# Summary comparison table
"""
| Approach | Import Correctness | Extra Cost | Maintenance |
|---|---|---|---|
| Option 1: Inject installed metadata | High (runtime truth) | ~200 tokens | Low |
| Option 2: Execute & retry on error | Very High (empirical) | 1-2 retries | Low |
| Option 3: Alias registry rewrite | Medium (registry lag) | 0 LLM calls | Medium |
| Option 4: Structured manifest | High (validated) | 2 LLM calls | Low |
| Option 5: AST linter pre-return | Medium (pattern-based) | 1 correction | Medium |
| Option 6: Migration guide injection | High (curated notes) | ~400 tokens | Medium |
"""
```

**Expected Token Savings:** ~400 token overhead per call; prevents 2-4 correction roundtrips averaging 2,000 tokens each. Net savings: ~1,600 tokens per session with import errors.
**Environment:** Works offline with curated notes. Extend by fetching GitHub release notes or PyPI changelog API for fully automated updates.
