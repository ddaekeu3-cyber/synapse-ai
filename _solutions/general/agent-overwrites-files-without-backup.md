---
layout: solution
title: "Agent Overwrites Files Without Creating Backups"
category: general
description: "Agent edits configuration files, source code, or data files in-place — no backup, no version tracking, no rollback path. When a run fails midway, the file is left in a corrupt or partially-written state. The original is gone."
tags: [general, safety, backup, rollback, filesystem, atomic-write, idempotency]
---

## Symptom

Agent is asked to update a YAML configuration file. It reads the file, generates new content, and writes it back. The API call times out halfway through writing. The config file now contains truncated YAML — neither the old nor the new version. The service fails to start. The original config is unrecoverable. Engineers spend two hours reconstructing it from memory.

File corruption rate without atomic writes: **5–15%** (network timeouts, OOM kills, disk-full conditions)
With atomic write + backup: **~0%** — original always preserved

## Root Cause

The agent uses `open(path, "w").write(content)` — a direct in-place overwrite. If the process dies during the write, the file is truncated. If the content is wrong, the original is already gone. There is no backup, no staging file, and no rollback mechanism.

## Fix

---

### Option 1 — Atomic Write via Temp File + Rename

Write to a temporary file in the same directory, then `os.replace()` it into place. The rename is atomic on POSIX — the file either has the old content or the new content, never a partial state.

```python
import os
import tempfile
import anthropic
from pathlib import Path

client = anthropic.Anthropic()

def atomic_write(path: str, content: str, encoding: str = "utf-8") -> None:
    """
    Write content to path atomically.
    Uses write-to-temp + rename — the destination is never partially written.
    Temp file is in the same directory to ensure same-filesystem rename.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory (same filesystem = atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # Ensure data is flushed to disk

        os.replace(tmp_path, path)   # Atomic rename — POSIX guarantee
        print(f"[Write] Atomically wrote {len(content)} bytes to {path}")
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def atomic_write_with_backup(path: str, content: str) -> str:
    """
    Create a timestamped backup of the current file, then atomic-write the new content.
    Returns the backup path.
    """
    import time
    target = Path(path)
    backup_path = None

    if target.exists():
        timestamp = int(time.time())
        backup_path = str(target.with_suffix(f".bak.{timestamp}{target.suffix}"))
        import shutil
        shutil.copy2(path, backup_path)
        print(f"[Backup] {path} → {backup_path}")

    atomic_write(path, content)
    return backup_path

# Example: agent updates a config file
def update_config_file(config_path: str, user_instruction: str) -> str:
    # Read current config
    try:
        with open(config_path) as f:
            current_content = f.read()
    except FileNotFoundError:
        current_content = ""

    # Ask Claude to generate the updated config
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a configuration file editor. Output ONLY the complete updated file content. No explanations, no markdown fences.",
        messages=[{
            "role": "user",
            "content": f"Current file:\n{current_content}\n\nInstruction: {user_instruction}",
        }],
    )
    new_content = response.content[0].text

    # Write atomically with backup
    backup = atomic_write_with_backup(config_path, new_content)
    return backup or ""

# Demo
import tempfile, os

with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
    f.write("server:\n  port: 8080\n  host: localhost\nlogging:\n  level: info\n")
    config_path = f.name

print(f"Config path: {config_path}")
print(f"Original: {open(config_path).read()}")

backup_path = update_config_file(config_path, "Change the logging level to debug and add a timeout of 30s")
print(f"\nUpdated: {open(config_path).read()}")
if backup_path:
    print(f"Backup at: {backup_path}")
    print(f"Original preserved: {open(backup_path).read()}")

os.unlink(config_path)
if backup_path and os.path.exists(backup_path):
    os.unlink(backup_path)
```

**Expected Token Savings:** None — same tokens; eliminates irreversible file corruption
**Environment:** `pip install anthropic`

---

### Option 2 — Versioned Backup Store with Rollback Tool

Give the agent a `rollback_file` tool. Before every write, save a numbered version. If the result is wrong, the agent (or the user) can roll back to any prior version.

```python
import json
import os
import shutil
import tempfile
import time
import anthropic
from pathlib import Path

client = anthropic.Anthropic()

class VersionedFileStore:
    """Keeps up to MAX_VERSIONS numbered backups per file."""
    MAX_VERSIONS = 10

    def __init__(self, backup_dir: str = "/tmp/agent_backups"):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _backup_prefix(self, path: str) -> Path:
        safe_name = Path(path).name.replace("/", "_")
        return self.backup_dir / safe_name

    def save_version(self, path: str) -> int:
        """Save current file as a new version. Returns version number."""
        if not os.path.exists(path):
            return 0

        prefix = self._backup_prefix(path)
        # Find next version number
        existing = sorted(
            [int(p.stem.split(".v")[-1]) for p in self.backup_dir.glob(f"{prefix.name}.v*") if ".v" in p.stem],
        )
        version = (existing[-1] + 1) if existing else 1

        backup_path = Path(f"{prefix}.v{version:04d}")
        shutil.copy2(path, backup_path)
        print(f"[Version] Saved v{version:04d} ← {path}")

        # Prune old versions
        all_versions = sorted(self.backup_dir.glob(f"{prefix.name}.v*"))
        for old in all_versions[:-self.MAX_VERSIONS]:
            old.unlink()

        return version

    def list_versions(self, path: str) -> list[dict]:
        prefix = self._backup_prefix(path)
        versions = []
        for p in sorted(self.backup_dir.glob(f"{prefix.name}.v*")):
            stat = p.stat()
            version_num = int(p.stem.split(".v")[-1])
            versions.append({
                "version": version_num,
                "path": str(p),
                "size_bytes": stat.st_size,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            })
        return versions

    def restore_version(self, path: str, version: int) -> bool:
        prefix = self._backup_prefix(path)
        backup_path = Path(f"{prefix}.v{version:04d}")
        if not backup_path.exists():
            return False
        shutil.copy2(backup_path, path)
        print(f"[Restore] Restored v{version:04d} → {path}")
        return True

store = VersionedFileStore()

def write_file(path: str, content: str) -> str:
    store.save_version(path)  # Always version before write
    with open(path, "w") as f:
        f.write(content)
    return json.dumps({"written": True, "path": path, "size": len(content)})

def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return json.dumps({"content": f.read(), "path": path})
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})

def list_versions(path: str) -> str:
    versions = store.list_versions(path)
    return json.dumps({"path": path, "versions": versions, "count": len(versions)})

def rollback_file(path: str, version: int) -> str:
    success = store.restore_version(path, version)
    if success:
        return json.dumps({"restored": True, "version": version, "path": path})
    return json.dumps({"error": f"Version {version} not found for {path}"})

TOOLS = [
    {"name": "read_file", "description": "Read a file's current content.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file",
     "description": "Write content to a file. ALWAYS creates a versioned backup before writing.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "list_versions", "description": "List all backup versions for a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "rollback_file", "description": "Restore a file to a previous version.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "version": {"type": "integer"}},
                      "required": ["path", "version"]}},
]

TOOL_MAP = {"read_file": read_file, "write_file": write_file,
            "list_versions": list_versions, "rollback_file": rollback_file}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP[block.name]
                result = fn(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Create a test file and run the agent
test_path = "/tmp/agent_test_config.txt"
with open(test_path, "w") as f:
    f.write("version: 1\ndebug: false\nmax_workers: 4\n")

print(run_agent(f"Read {test_path}, add a 'timeout: 30' line, and write it back. Then list its versions."))
```

**Expected Token Savings:** None — same tokens; enables rollback when AI-generated content is wrong
**Environment:** `pip install anthropic`

---

### Option 3 — Git-Based File Version Control

Commit every agent-modified file to a local git repository. Full history, diff support, and standard `git checkout` rollback — no custom tooling needed.

```python
import json
import os
import subprocess
import tempfile
import anthropic
from pathlib import Path

client = anthropic.Anthropic()

class GitFileManager:
    def __init__(self, repo_path: str):
        self.repo = Path(repo_path)
        self.repo.mkdir(parents=True, exist_ok=True)
        self._git("init")
        self._git("config", "user.email", "agent@synapse.ai")
        self._git("config", "user.name", "Agent")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True
        )
        return result.stdout.strip()

    def write(self, relative_path: str, content: str, commit_message: str = "Agent edit") -> dict:
        """Write a file and commit it to git."""
        full_path = self.repo / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        with open(full_path, "w") as f:
            f.write(content)

        self._git("add", relative_path)
        self._git("commit", "-m", commit_message, "--allow-empty")
        commit_hash = self._git("rev-parse", "--short", "HEAD")

        return {"written": True, "path": relative_path, "commit": commit_hash}

    def read(self, relative_path: str) -> str:
        full_path = self.repo / relative_path
        if not full_path.exists():
            return ""
        with open(full_path) as f:
            return f.read()

    def history(self, relative_path: str) -> list[dict]:
        log = self._git("log", "--oneline", "--", relative_path)
        commits = []
        for line in log.splitlines():
            if " " in line:
                hash_, message = line.split(" ", 1)
                commits.append({"hash": hash_, "message": message})
        return commits

    def rollback(self, relative_path: str, commit_hash: str) -> bool:
        self._git("checkout", commit_hash, "--", relative_path)
        self._git("commit", "-m", f"Rollback {relative_path} to {commit_hash}")
        return True

    def diff(self, relative_path: str) -> str:
        return self._git("diff", "HEAD~1", "HEAD", "--", relative_path)

git_mgr = GitFileManager("/tmp/agent_git_repo")

def read_file(path: str) -> str:
    content = git_mgr.read(path)
    return json.dumps({"content": content, "path": path, "exists": bool(content)})

def write_file(path: str, content: str, commit_message: str = "Agent edit") -> str:
    result = git_mgr.write(path, content, commit_message)
    return json.dumps(result)

def get_file_history(path: str) -> str:
    history = git_mgr.history(path)
    return json.dumps({"path": path, "history": history})

def rollback_to_commit(path: str, commit_hash: str) -> str:
    success = git_mgr.rollback(path, commit_hash)
    return json.dumps({"rolled_back": success, "path": path, "commit": commit_hash})

TOOLS = [
    {"name": "read_file", "description": "Read file content from the git-managed workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file",
     "description": "Write file content. Automatically commits to git with a message.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                     "commit_message": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "get_file_history", "description": "Get git commit history for a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "rollback_to_commit", "description": "Roll back a file to a specific git commit.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "commit_hash": {"type": "string"}},
                      "required": ["path", "commit_hash"]}},
]

TOOL_MAP = {
    "read_file": read_file, "write_file": write_file,
    "get_file_history": get_file_history, "rollback_to_commit": rollback_to_commit,
}

# Seed a file
git_mgr.write("config/app.json",
               json.dumps({"port": 8080, "debug": False, "workers": 4}, indent=2),
               "Initial config")

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = TOOL_MAP[block.name](**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

result = run_agent("Read config/app.json, set workers to 8 and enable debug mode, write it back with message 'Enable debug for testing', then show its history.")
print(result)
```

**Expected Token Savings:** None — same tokens; full audit trail and one-command rollback
**Environment:** `pip install anthropic git`

---

### Option 4 — Shadow Write with Validation Before Commit

Write the new content to a shadow path, validate it (syntax check, schema validation), and only replace the real file if validation passes.

```python
import json
import os
import yaml
import tempfile
import anthropic
from pathlib import Path
from typing import Callable, Optional

client = anthropic.Anthropic()

class ValidationError(Exception):
    pass

def validate_json(content: str) -> None:
    json.loads(content)  # Raises json.JSONDecodeError if invalid

def validate_yaml(content: str) -> None:
    yaml.safe_load(content)  # Raises yaml.YAMLError if invalid

def validate_python_syntax(content: str) -> None:
    compile(content, "<string>", "exec")  # Raises SyntaxError if invalid

# Map file extensions to validators
VALIDATORS: dict[str, Callable[[str], None]] = {
    ".json": validate_json,
    ".yaml": validate_yaml,
    ".yml":  validate_yaml,
    ".py":   validate_python_syntax,
}

def shadow_write(
    path: str,
    content: str,
    extra_validator: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    1. Write content to a shadow temp file
    2. Run format validator (JSON/YAML/Python syntax)
    3. Run optional custom validator
    4. Only if all pass: atomically replace the real file
    """
    target = Path(path)
    ext = target.suffix.lower()
    validator = VALIDATORS.get(ext)

    # Step 1: Write to shadow
    fd, shadow_path = tempfile.mkstemp(
        dir=target.parent if target.parent.exists() else "/tmp",
        prefix=".shadow_",
        suffix=target.suffix,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)

        # Step 2: Format validation
        if validator:
            try:
                validator(content)
                print(f"[Validate] Format OK ({ext})")
            except Exception as e:
                raise ValidationError(f"Format validation failed: {e}")

        # Step 3: Custom validator
        if extra_validator:
            try:
                extra_validator(content)
                print("[Validate] Custom validation OK")
            except Exception as e:
                raise ValidationError(f"Custom validation failed: {e}")

        # Step 4: Atomic replace
        os.replace(shadow_path, path)
        print(f"[Write] {path} updated ({len(content)} bytes)")
        return {"written": True, "path": path, "validated": True}

    except Exception as e:
        try:
            os.unlink(shadow_path)
        except OSError:
            pass
        if isinstance(e, ValidationError):
            return {"written": False, "error": str(e), "content_rejected": True}
        raise

def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return json.dumps({"content": f.read()})
    except FileNotFoundError:
        return json.dumps({"content": "", "exists": False})

def write_validated_file(path: str, content: str) -> str:
    """Write with automatic format validation."""
    # Optional: add custom business-logic validators here
    def check_required_keys(c: str):
        ext = Path(path).suffix.lower()
        if ext == ".json":
            data = json.loads(c)
            # Example: config files must have 'version' key
            if "config" in path and "version" not in data:
                raise ValueError("Config JSON must contain 'version' key")

    result = shadow_write(path, content, extra_validator=check_required_keys)
    return json.dumps(result)

TOOLS = [
    {"name": "read_file", "description": "Read a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_validated_file",
     "description": "Write a file. Validates syntax (JSON/YAML/Python) before saving. Rejects invalid content.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = {"read_file": read_file, "write_validated_file": write_validated_file}[block.name]
                result = fn(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Test: agent writes valid JSON then tries invalid JSON
config_path = "/tmp/test_agent_config.json"
with open(config_path, "w") as f:
    json.dump({"version": "1.0", "port": 8080}, f)

print("=== Valid update ===")
print(run_agent(f"Read {config_path} and add a 'timeout': 30 field, then write it back."))
print("\n=== Invalid update (should be rejected) ===")
print(run_agent(f"Write this to {config_path}: {{invalid json here: missing quote}}"))
```

**Expected Token Savings:** None — catches corrupt output before it reaches disk; one fewer recovery run
**Environment:** `pip install anthropic pyyaml`

---

### Option 5 — Content Hash Idempotency Guard

Before writing, hash the new content. If it matches the current file, skip the write. If it differs, back up and write. Makes write operations idempotent — safe to retry.

```python
import hashlib
import json
import os
import shutil
import time
import anthropic
from pathlib import Path

client = anthropic.Anthropic()

def file_hash(path: str) -> str:
    """SHA-256 of file content, or empty string if file doesn't exist."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return ""

def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()

def idempotent_write(path: str, content: str, backup: bool = True) -> dict:
    """
    Write only if content differs from current file.
    Returns action taken: 'skipped' | 'written' | 'backed_up_and_written'.
    """
    new_hash = content_hash(content)
    current_hash = file_hash(path)

    if new_hash == current_hash:
        print(f"[Idempotent] {path}: content unchanged — skip write")
        return {"action": "skipped", "path": path, "hash": new_hash}

    backup_path = None
    if backup and os.path.exists(path):
        timestamp = int(time.time())
        backup_path = f"{path}.bak.{timestamp}"
        shutil.copy2(path, backup_path)
        print(f"[Backup] {path} → {backup_path}")

    # Atomic write
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=Path(path).parent, prefix=".tmp_", suffix=Path(path).suffix)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)

    print(f"[Write] {path} updated (hash {new_hash[:8]}...)")
    return {
        "action": "backed_up_and_written" if backup_path else "written",
        "path": path,
        "backup": backup_path,
        "hash": new_hash,
    }

def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return json.dumps({"content": f.read(), "hash": file_hash(path)})
    except FileNotFoundError:
        return json.dumps({"content": "", "exists": False})

def write_file(path: str, content: str) -> str:
    result = idempotent_write(path, content)
    return json.dumps(result)

TOOLS = [
    {"name": "read_file", "description": "Read a file and its content hash.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file",
     "description": "Write a file. Skips if content is unchanged (idempotent). Backs up if content differs.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = {"read_file": read_file, "write_file": write_file}[block.name]
                result = fn(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

test_path = "/tmp/idempotent_test.txt"
with open(test_path, "w") as f:
    f.write("Hello, world!\n")

print("=== First run (should write) ===")
print(run_agent(f"Read {test_path} and append ' Updated by agent.' to the content, then write it back."))
print("\n=== Second run (same instruction — should skip if result is identical) ===")
print(run_agent(f"Read {test_path} and write back exactly what you read, unchanged."))
```

**Expected Token Savings:** None — same tokens; prevents redundant writes on retry
**Environment:** `pip install anthropic`

---

### Option 6 — Two-Step Write with Dry-Run Preview

Before overwriting any file, output a dry-run diff for user review. Only proceed with the actual write after showing what will change.

```python
import json
import os
import difflib
import tempfile
import anthropic

client = anthropic.Anthropic()

_pending_writes: dict[str, str] = {}   # path → staged content

def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return json.dumps({"content": f.read(), "path": path})
    except FileNotFoundError:
        return json.dumps({"content": "", "path": path, "exists": False})

def stage_file_write(path: str, new_content: str) -> str:
    """
    Stage a write operation and return a unified diff for review.
    Does NOT write yet — call commit_staged_write to apply.
    """
    try:
        with open(path) as f:
            current = f.readlines()
    except FileNotFoundError:
        current = []

    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        current, new_lines,
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
        lineterm="",
    ))

    if not diff:
        return json.dumps({"staged": False, "reason": "No changes — content identical to current file"})

    _pending_writes[path] = new_content
    diff_text = "\n".join(diff[:50])  # Show first 50 lines of diff
    truncated = len(diff) > 50

    return json.dumps({
        "staged": True,
        "path": path,
        "lines_changed": len([l for l in diff if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]),
        "diff_preview": diff_text + ("\n... (truncated)" if truncated else ""),
        "instruction": "Review the diff above. Call commit_staged_write to apply, or discard_staged_write to cancel.",
    })

def commit_staged_write(path: str) -> str:
    """Apply a previously staged write after user/agent review."""
    if path not in _pending_writes:
        return json.dumps({"error": f"No staged write for {path}. Call stage_file_write first."})

    content = _pending_writes.pop(path)

    # Backup current
    import shutil, time
    if os.path.exists(path):
        backup = f"{path}.bak.{int(time.time())}"
        shutil.copy2(path, backup)

    # Atomic write
    import tempfile
    from pathlib import Path
    fd, tmp = tempfile.mkstemp(dir=Path(path).parent if Path(path).parent.exists() else "/tmp")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)

    return json.dumps({"committed": True, "path": path, "bytes": len(content)})

def discard_staged_write(path: str) -> str:
    """Discard a staged write without applying it."""
    if path in _pending_writes:
        del _pending_writes[path]
        return json.dumps({"discarded": True, "path": path})
    return json.dumps({"error": f"No staged write for {path}"})

TOOLS = [
    {"name": "read_file", "description": "Read a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "stage_file_write",
     "description": "Preview changes to a file as a diff. Does NOT write yet — requires commit_staged_write to apply.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "new_content": {"type": "string"}},
                      "required": ["path", "new_content"]}},
    {"name": "commit_staged_write",
     "description": "Apply a previously staged write after reviewing the diff.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "discard_staged_write",
     "description": "Cancel a staged write without making any changes.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]

TOOL_MAP = {
    "read_file": read_file,
    "stage_file_write": stage_file_write,
    "commit_staged_write": commit_staged_write,
    "discard_staged_write": discard_staged_write,
}

SYSTEM = """You are a careful file editor.
Always stage changes first (stage_file_write) and confirm the diff looks correct before committing.
Never skip the staging step for file edits."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = TOOL_MAP[block.name](**block.input)
                print(f"[Tool] {block.name}: {json.loads(result).get('staged', json.loads(result).get('committed', ''))}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

test_path = "/tmp/staged_write_test.py"
with open(test_path, "w") as f:
    f.write("def greet(name):\n    print('Hello ' + name)\n\ngreet('World')\n")

print(run_agent(f"Read {test_path}, update the greet function to use an f-string, then apply the change."))
```

**Expected Token Savings:** None — same tokens; catches wrong edits before they overwrite source files
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Protection Level | Rollback Method | Best For |
|--------|-----------------|----------------|----------|
| Atomic Temp+Rename | Corruption prevention | None (manual backup) | All file writes — mandatory baseline |
| Versioned Backup Store | Full version history | `rollback_file` tool | Long-running agents editing config |
| Git-Based Versioning | Full diff history | `git checkout` | Code-editing agents |
| Shadow Write + Validation | Content validation | Auto-reject on failure | Config/JSON/YAML files |
| Content Hash Idempotency | Skip unchanged writes | N/A — idempotent | Agents that retry on failure |
| Two-Step Dry-Run | Human/agent review | Discard staging | Destructive or complex edits |

**Recommended starting point:** Option 1 (Atomic Write via Temp+Rename) — replace every `open(path, "w").write()` with `atomic_write(path, content)`. This is a 5-line change that eliminates file corruption from crashes or timeouts. Add Option 2 (Versioned Backup) when the agent edits files the user cares about recovering.
