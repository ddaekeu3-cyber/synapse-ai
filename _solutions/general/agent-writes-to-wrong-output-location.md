---
layout: solution
title: "Agent Writes Output to Wrong File or Directory"
category: general
description: "Agent generates a report and saves it to the current working directory instead of the output folder. Or overwrites an existing file. Or creates output in a temporary path that gets deleted. Output is lost."
tags: [file-output, working-directory, overwrite, path, output, data-loss]
---

# Agent Writes Output to Wrong File or Directory

## Symptom
- Agent reports "saved to report.csv" but file is in wrong directory
- Output overwrites existing data — data loss
- File created in `/tmp` or container path — gone after restart
- Agent saves to relative path that resolves differently in different environments
- Multiple runs create the same filename, last run overwrites all previous output

## Root Cause
Output paths not explicitly configured or validated. Agent uses current working directory (which varies by environment) or a default path without checking if it's the right location. No collision handling means repeated runs silently overwrite previous output.

## Fix

### Option 1: Explicit output directory configuration

```python
import os
from pathlib import Path
from datetime import datetime

# WRONG — output in CWD (varies by environment)
def save_report(data):
    open("report.csv", "w").write(data)  # Where is this? ¯\_(ツ)_/¯

# RIGHT — explicit, configured output directory
OUTPUT_DIR = Path(os.environ.get("AGENT_OUTPUT_DIR", Path.home() / "agent_output"))

def save_report(data: str, filename: str = None) -> Path:
    """Save to configured output directory with timestamp to avoid overwrites"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{timestamp}.csv"

    output_path = OUTPUT_DIR / filename

    # Prevent accidental overwrite
    if output_path.exists():
        backup = output_path.with_suffix(f".{timestamp}.bak")
        output_path.rename(backup)
        print(f"Existing file backed up to: {backup}")

    output_path.write_text(data)
    print(f"Saved to: {output_path}")
    return output_path
```

### Option 2: Confirm output path before writing

```python
from pathlib import Path

def save_with_confirmation(data: str, path: str, force: bool = False) -> Path:
    """Save file with confirmation of target location"""
    output_path = Path(path).resolve()  # Always show absolute path

    print(f"Output location: {output_path}")

    # Warn about potentially wrong locations
    if "/tmp" in str(output_path):
        print("WARNING: Writing to /tmp — this may be deleted on restart")

    if output_path.exists():
        if not force:
            response = input(f"File exists: {output_path}. Overwrite? [y/N]: ")
            if response.lower() != "y":
                raise FileExistsError(f"Aborted — will not overwrite {output_path}")
        else:
            print(f"Overwriting existing file: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(data)
    print(f"Written: {output_path} ({output_path.stat().st_size:,} bytes)")
    return output_path
```

### Option 3: Timestamped outputs to prevent collisions

```python
from pathlib import Path
from datetime import datetime
import hashlib

def generate_unique_output_path(
    base_name: str,
    output_dir: Path,
    strategy: str = "timestamp"
) -> Path:
    """Generate unique output path to prevent overwrites"""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix or ".txt"

    if strategy == "timestamp":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return output_dir / f"{stem}_{timestamp}{suffix}"

    elif strategy == "counter":
        counter = 1
        path = output_dir / base_name
        while path.exists():
            path = output_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        return path

    elif strategy == "hash":
        # Content-addressed: same content = same file, different content = new file
        content_hash = hashlib.md5(base_name.encode()).hexdigest()[:8]
        return output_dir / f"{stem}_{content_hash}{suffix}"

    return output_dir / base_name

# Usage
path = generate_unique_output_path("report.csv", OUTPUT_DIR, strategy="timestamp")
path.write_text(data)
# → /output/report_20250415_143022.csv
```

### Option 4: Validate output path before any long-running task

```python
from pathlib import Path
import os

def validate_output_configuration(output_config: dict) -> dict:
    """Validate output paths before starting task — fail fast"""
    errors = []
    validated = {}

    for key, path_str in output_config.items():
        path = Path(path_str).resolve()

        # Check parent directory is writable
        parent = path.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
                print(f"Created output directory: {parent}")
            except PermissionError:
                errors.append(f"{key}: Cannot create directory {parent}")
                continue

        if not os.access(parent, os.W_OK):
            errors.append(f"{key}: Directory not writable: {parent}")
            continue

        # Warn about volatile paths
        volatile_paths = ["/tmp", "/var/tmp", "/dev/shm"]
        if any(str(path).startswith(v) for v in volatile_paths):
            print(f"WARNING: {key} is in volatile storage ({path}) — data will not persist across restarts")

        validated[key] = path

    if errors:
        raise ValueError(f"Output configuration errors:\n" + "\n".join(errors))

    return validated

# Before running the agent:
paths = validate_output_configuration({
    "report": "/data/output/report.csv",
    "logs": "/data/logs/agent.log",
    "checkpoints": "/data/checkpoints/",
})
```

### Option 5: Output manifest — track all files written

```python
import json
from pathlib import Path
from datetime import datetime

class OutputManifest:
    """Track all files written by agent for auditability"""

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.entries = []

    def record(self, path: Path, description: str, size: int = None):
        entry = {
            "path": str(path.resolve()),
            "description": description,
            "written_at": datetime.utcnow().isoformat(),
            "size_bytes": size or (path.stat().st_size if path.exists() else 0)
        }
        self.entries.append(entry)
        self.manifest_path.write_text(json.dumps(self.entries, indent=2))
        print(f"Output: {path.resolve()} ({entry['size_bytes']:,} bytes) — {description}")

    def print_summary(self):
        print(f"\n=== Output Summary ({len(self.entries)} files) ===")
        for entry in self.entries:
            print(f"  {entry['path']}")
            print(f"    → {entry['description']} ({entry['size_bytes']:,} bytes)")

manifest = OutputManifest(Path("./agent_run_manifest.json"))

# After each write:
report_path = OUTPUT_DIR / "report.csv"
report_path.write_text(report_data)
manifest.record(report_path, "Monthly usage report", len(report_data))
```

### Option 6: System prompt for output clarity

```
System prompt:
"Output file rules:
1. Always use the absolute output directory: {OUTPUT_DIR}
2. Never write to /tmp — files there don't persist
3. Never write to the current working directory unless explicitly told to
4. Add timestamps to filenames to avoid overwriting: report_20250415.csv
5. Before writing, confirm the full absolute path in your response
6. After writing, confirm the file was created and report its size"
```

## Common Output Path Mistakes

| Mistake | Problem | Fix |
|---|---|---|
| Writing to CWD | Path varies by environment | Use configured OUTPUT_DIR |
| Writing to /tmp | Deleted on restart | Use persistent volume |
| No timestamp in name | Overwrites previous runs | Add timestamp suffix |
| Relative path | Different location per caller | Always resolve to absolute |
| No permission check | Fails mid-task | Validate at startup |
| No overwrite check | Silently loses data | Check before writing |

## Expected Token Savings
Data loss + debugging + re-running: ~20,000 tokens
Output path validation prevents it: 0 wasted

## Environment
- Any agent writing output files; critical in batch processing and report generation agents
- Source: direct experience with output file management in production agents
