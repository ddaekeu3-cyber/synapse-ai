---
layout: solution
title: "Agent Hallucinates File Paths That Don't Exist"
category: hallucination
description: "Agent confidently references, reads from, or writes to file paths it invents from context — paths that do not exist in the actual filesystem — causing silent failures, wrong outputs, or downstream errors."
tags: [hallucination, file-system, tool-use, grounding, verification, reliability]
---

## Symptom

A developer asks the agent to "update the logging configuration." The agent replies: "I've updated `/src/config/logging.yaml` with the new settings." No such file exists; the agent invented the path from a plausible convention. The user commits the change, deploys, and discovers the real config file was never touched. In another case, the agent reads `app/models/user.py` and fabricates its contents rather than actually calling the read tool, producing a summary of code that does not match the real file.

## Root Cause

LLMs learn filesystem conventions from training data (e.g., "Python projects often have `src/config/`") and confabulate plausible paths rather than grounding them in tool calls. This happens when: (1) the agent is asked about files without being required to verify their existence first; (2) the agent's tool call results are not strictly checked before being used in the next step; or (3) the system prompt instructs the agent to "be helpful" without requiring it to verify facts before stating them.

## Fix

### Option 1 — Require path verification tool before any file reference

```python
import os
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "list_directory",
        "description": "List files in a directory. ALWAYS call this before referencing any file path to verify the path exists.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a file. Only call this after list_directory confirms the file exists.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
            },
        },
    },
    {
        "name": "check_path_exists",
        "description": "Check whether a file or directory exists before referencing it.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Path to check"},
            },
        },
    },
]

SYSTEM = """You are a filesystem assistant.

CRITICAL RULES:
1. NEVER reference, describe, or summarise a file without first calling check_path_exists or list_directory.
2. If a path does not exist, say so explicitly — do not invent alternative paths.
3. NEVER invent file contents — only read_file provides ground truth.
4. If a user asks you to modify a file, first verify it exists, then read it, then describe the change."""

def handle_tool(name: str, inputs: dict) -> str:
    if name == "list_directory":
        path = inputs["path"]
        try:
            entries = os.listdir(path)
            return json.dumps({"path": path, "entries": entries})
        except FileNotFoundError:
            return json.dumps({"error": f"Directory not found: {path}"})

    elif name == "check_path_exists":
        path = inputs["path"]
        return json.dumps({"path": path, "exists": os.path.exists(path)})

    elif name == "read_file":
        path = inputs["path"]
        try:
            with open(path) as f:
                content = f.read(500)
            return json.dumps({"path": path, "content": content})
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}"})

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                print(f"  [tool] {b.name}({b.input}) → {result[:80]}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

questions = [
    "What's in the logging config file for this project?",
    f"List the files in {os.getcwd()} and tell me what the main Python file does.",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)[:300]}\n")
```

**Expected Token Savings:** Path verification adds 1-2 tool calls (~50 tokens each) but prevents the agent from confabulating file contents — a hallucinated file update that goes undetected can cause production incidents worth hours of debugging.
**Environment:** All agents with filesystem access; requiring explicit path verification before any file reference is the most reliable way to prevent file-path hallucination.

---

### Option 2 — Ground-truth path injection: supply real directory tree in context

```python
import os
import anthropic

client = anthropic.Anthropic()

def get_directory_tree(root: str, max_depth: int = 3, max_files: int = 50) -> str:
    """Build a compact directory tree to inject into the system prompt."""
    lines   = []
    count   = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden and common noise directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv")]
        depth = dirpath.replace(root, "").count(os.sep)
        if depth >= max_depth:
            dirnames.clear()
            continue
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(dirpath)}/")
        for filename in filenames:
            if count >= max_files:
                lines.append(f"{indent}  ... (truncated)")
                return "\n".join(lines)
            lines.append(f"{indent}  {filename}")
            count += 1
    return "\n".join(lines)

def build_system(project_root: str) -> str:
    tree = get_directory_tree(project_root)
    return f"""You are a code assistant.

The actual file tree for this project is:
```
{tree}
```

RULES:
- Only reference files that appear in the tree above.
- If a user asks about a file not in the tree, say "that file does not exist" — do not invent it.
- Never describe or summarise file contents you haven't been given."""

def ask(question: str, project_root: str = ".") -> str:
    system = build_system(project_root)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text

# Test with real current directory
cwd = os.getcwd()
questions = [
    "What configuration files does this project have?",
    "Is there a file called main.py in this project?",
    "Where is the logging configuration stored?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q, cwd)[:250]}\n")
```

**Expected Token Savings:** Injecting the real directory tree (50-200 tokens) eliminates all file-path hallucinations for the listed files; the model can only reference paths that exist in the tree, converting hallucination from a runtime failure to a prompt-time constraint.
**Environment:** Agents working within a known, bounded project directory; tree injection is the simplest solution when the directory is small enough to fit in the system prompt.

---

### Option 3 — Post-generation path validator: scan responses for invented paths

```python
import re
import os
import anthropic

client = anthropic.Anthropic()

# Regex to extract file paths from agent responses
PATH_PATTERN = re.compile(
    r'(?:^|[\s`"\'])(/[\w./\-]+\.\w+|[\w./\-]+/[\w./\-]+\.\w+)',
    re.MULTILINE,
)

def extract_paths(text: str) -> list[str]:
    return [m.group(1) for m in PATH_PATTERN.finditer(text)]

def validate_paths(text: str, project_root: str = ".") -> dict:
    paths   = extract_paths(text)
    results = {}
    for path in paths:
        # Check absolute path and relative to project root
        abs_path = path if os.path.isabs(path) else os.path.join(project_root, path)
        results[path] = os.path.exists(abs_path)
    return results

def ask_with_path_check(question: str, project_root: str = ".") -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": question}],
    )
    response = r.content[0].text

    # Check all referenced paths
    path_validity = validate_paths(response, project_root)
    invented = [p for p, exists in path_validity.items() if not exists]

    if invented:
        print(f"  [WARNING] Agent referenced non-existent paths: {invented}")
        # Append a correction note
        correction = (
            f"\n\n⚠️ **Note:** The following paths do not exist in this project "
            f"and were hallucinated: {invented}. Please verify file locations before acting."
        )
        return response + correction

    if path_validity:
        print(f"  [OK] All referenced paths verified: {list(path_validity.keys())}")

    return response

questions = [
    "Tell me about the logging configuration in this Python project.",
    "What does the main application entry point do?",
    f"What files are in {os.getcwd()}?",
]
for q in questions:
    print(f"Q: {q}")
    r = ask_with_path_check(q, project_root=os.getcwd())
    print(f"A: {r[:300]}\n")
```

**Expected Token Savings:** Post-generation path validation adds zero tokens to the LLM call; a simple `os.path.exists()` check on every path mentioned in the response catches hallucinated paths and appends a warning before the user acts on incorrect information.
**Environment:** Agents where retrofitting tool-based verification is expensive; path scanning is a lightweight guardrail that can be added to any existing agent as a post-processing step.

---

### Option 4 — Two-stage approach: discover then act

```python
import os
import json
import glob
import anthropic

client = anthropic.Anthropic()

DISCOVER_TOOLS = [
    {
        "name": "find_files",
        "description": "Search for files matching a pattern. Use this FIRST to discover real paths before referencing them.",
        "input_schema": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.yaml' or '**/config*'"},
                "root":    {"type": "string", "description": "Search root directory", "default": "."},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a file that was found via find_files.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
]

SYSTEM = """You are a filesystem assistant operating in two stages:
Stage 1 — DISCOVER: Use find_files to locate real files matching the user's intent.
Stage 2 — ACT: Only reference or read files returned by find_files in Stage 1.

NEVER reference a path you did not discover via find_files."""

def handle_tool(name: str, inputs: dict) -> str:
    if name == "find_files":
        pattern = inputs["pattern"]
        root    = inputs.get("root", ".")
        try:
            matches = glob.glob(os.path.join(root, pattern), recursive=True)[:20]
            return json.dumps({"matches": matches, "count": len(matches)})
        except Exception as e:
            return json.dumps({"error": str(e)})
    elif name == "read_file":
        path = inputs["path"]
        try:
            with open(path) as f:
                return json.dumps({"content": f.read(800)})
        except FileNotFoundError:
            return json.dumps({"error": f"Not found: {path}"})

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(10):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=DISCOVER_TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                print(f"  [{b.name}] {b.input} → {result[:100]}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(f"Q: Find the Python files in {os.getcwd()} and describe them.")
print(f"A: {run_agent(f'Find the Python files in {os.getcwd()} and describe them.')[:400]}")
```

**Expected Token Savings:** Two-stage discover-then-act forces the agent to ground every file reference in actual `glob` results; the discover stage costs ~50 tokens and prevents any hallucinated path from reaching the act stage, eliminating the downstream costs of acting on invented paths.
**Environment:** Agents that browse and summarise codebases; the discover-then-act pattern is especially valuable when the project structure is unknown and path conventions vary.

---

### Option 5 — Confidence gating: require agent to flag uncertain paths

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a filesystem assistant.

For every file path you mention, append a confidence marker:
- [VERIFIED] — you confirmed this path via a tool call in this conversation
- [INFERRED] — you are inferring this path from convention or context; it may not exist
- [UNKNOWN]  — you have no basis for this path

If a path is [INFERRED] or [UNKNOWN], you MUST call check_path_exists before proceeding.
Never take action (read, write, describe contents) on an [INFERRED] or [UNKNOWN] path."""

TOOLS = [
    {
        "name": "check_path_exists",
        "description": "Verify whether a file path exists on the filesystem.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
]

def handle_tool(name: str, inputs: dict) -> str:
    import os
    if name == "check_path_exists":
        path = inputs["path"]
        return json.dumps({"path": path, "exists": os.path.exists(path)})
    return json.dumps({"error": "unknown tool"})

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                print(f"  [check_path_exists] {b.input['path']} → {result}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

questions = [
    "Where would I find the database migration scripts in a typical Django project?",
    "What logging configuration does this project use?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)[:400]}\n")
```

**Expected Token Savings:** Confidence markers make path uncertainty explicit in every response; users immediately see which paths are verified vs. inferred, preventing them from acting on hallucinated paths without the agent needing to verify every single one upfront.
**Environment:** Agents answering general filesystem questions where some paths are definitively known and others are conventions; confidence markers are more efficient than verifying every path when most are timeless conventions (e.g., `/etc/hosts`).

---

### Option 6 — Assertion step: agent must state evidence for every path claim

```python
import anthropic

client = anthropic.Anthropic()

ANSWER_SYSTEM = "Answer the question about this project's file structure."

ASSERTION_SYSTEM = """You are a strict fact-checker reviewing an agent's response about file paths.

For each file path mentioned in the response, determine:
1. Is this path based on a tool call result in the conversation? → mark GROUNDED
2. Is this path inferred from naming conventions? → mark INFERRED (risky)
3. Is this path fabricated with no basis? → mark HALLUCINATED (dangerous)

Return JSON:
{
  "paths": [
    {"path": "/example/path", "status": "GROUNDED|INFERRED|HALLUCINATED", "evidence": "..."}
  ],
  "risk": "low|medium|high",
  "safe_to_act": true|false
}"""

def ask_with_assertion(question: str) -> dict:
    import json

    # Step 1: get initial answer
    r1 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    answer = r1.content[0].text

    # Step 2: fact-check paths in the answer
    check_prompt = f"Question: {question}\n\nAgent's answer:\n{answer}"
    r2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=ASSERTION_SYSTEM,
        messages=[{"role": "user", "content": check_prompt}],
    )
    raw = r2.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        analysis = {"paths": [], "risk": "unknown", "safe_to_act": False}

    return {"answer": answer, "analysis": analysis}

questions = [
    "Where is the main configuration file for this FastAPI application?",
    "What is 2 + 2?",   # no paths — should show low risk
]

for q in questions:
    print(f"Q: {q}")
    result = ask_with_assertion(q)
    print(f"A: {result['answer'][:200]}")
    analysis = result["analysis"]
    print(f"  [risk={analysis.get('risk')} safe={analysis.get('safe_to_act')}]")
    for p in analysis.get("paths", []):
        print(f"  {p.get('status'):12s} {p.get('path')} — {p.get('evidence','')[:60]}")
    print()
```

**Expected Token Savings:** Assertion step adds ~100 tokens per response but produces a machine-readable `safe_to_act` flag that downstream systems can use to gate file operations — prevents any hallucinated path from propagating into write operations or user-visible outputs.
**Environment:** High-stakes agents (automated code editing, deployment pipelines) where a hallucinated path that gets written to can cause data loss or broken deployments; assertion gating is the defensive-in-depth layer for critical file operations.

---

## Comparison

| Option | Prevents Hallucination | Adds Tool Calls | Runtime Cost | Best For |
|---|---|---|---|---|
| 1. Verification tool required | Yes (enforced) | Yes (1-2) | Low | General filesystem agents |
| 2. Tree injection | Yes (constrained) | No | None (prompt tokens) | Small bounded project dirs |
| 3. Post-gen path validator | Warns (not prevented) | No | Negligible (os.path) | Retrofit guardrail for existing agents |
| 4. Two-stage discover-then-act | Yes (enforced) | Yes (1+) | Low | Unknown project structures |
| 5. Confidence gating | Partially (user-visible) | Conditional | Low | Mixed known/unknown paths |
| 6. Assertion fact-checker | Yes (machine flag) | Yes (+1) | Medium | High-stakes write operations |
