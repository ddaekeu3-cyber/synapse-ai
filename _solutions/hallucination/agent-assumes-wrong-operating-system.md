---
layout: solution
title: "Agent Assumes Wrong Operating System — Linux Commands Fail on macOS or Windows"
category: hallucination
description: "Agent assumes Linux environment and runs Linux-only commands that fail on macOS or Windows. sed -i, apt-get, /proc/cpuinfo, or bash-specific syntax cause errors on the actual OS."
tags: [os, linux, macos, windows, cross-platform, hallucination]
---

# Agent Assumes Wrong Operating System — Linux Commands Fail on macOS or Windows

## Symptom
- `sed -i 's/old/new/' file` fails on macOS (requires `sed -i '' 's/old/new/' file`)
- `apt-get install` fails (macOS has Homebrew, not apt)
- `/proc/cpuinfo` doesn't exist (Linux-only)
- `nproc` command not found (macOS: `sysctl -n hw.ncpu`)
- Python path assumptions wrong: `/usr/bin/python3` vs `/opt/homebrew/bin/python3`
- Agent generates Docker commands assuming Linux when user is on Windows with WSL

## Root Cause
Training data is heavily Linux-weighted. The model defaults to Linux assumptions when the OS isn't specified, even if context clues suggest otherwise (e.g., macOS paths, Homebrew in history). The model doesn't automatically adjust command syntax for the actual target OS.

## Fix

### Option 1: Include OS in system prompt

```
System prompt:
"Environment: macOS Sequoia 15.x, Apple Silicon (arm64)
Shell: zsh
Package manager: Homebrew
Python: /opt/homebrew/bin/python3

When writing shell commands:
- Use BSD sed syntax (macOS): sed -i '' not sed -i
- Use brew not apt/yum
- Use sysctl -n hw.ncpu not nproc
- macOS paths: /opt/homebrew/ not /usr/local/ for Homebrew packages"
```

Or for Windows:
```
"Environment: Windows 11, PowerShell 7
Shell: PowerShell (not bash)
Package manager: winget or chocolatey
Use Windows path separators: C:\path\to\file not /path/to/file"
```

### Option 2: Detect OS in code and branch

```python
import platform, sys

OS = platform.system()  # 'Linux', 'Darwin' (macOS), 'Windows'
ARCH = platform.machine()  # 'x86_64', 'arm64', 'AMD64'

if OS == 'Darwin':
    SED_INPLACE = ['sed', '-i', '']   # macOS BSD sed
    PACKAGE_MGR = 'brew'
    CPU_COUNT_CMD = ['sysctl', '-n', 'hw.ncpu']
elif OS == 'Linux':
    SED_INPLACE = ['sed', '-i']       # GNU sed
    PACKAGE_MGR = 'apt-get'
    CPU_COUNT_CMD = ['nproc']
elif OS == 'Windows':
    PACKAGE_MGR = 'winget'
    CPU_COUNT_CMD = ['cmd', '/c', 'echo %NUMBER_OF_PROCESSORS%']
```

### Option 3: Use Python instead of shell commands for portability

```python
# BAD — OS-specific shell commands
import subprocess
subprocess.run(['sed', '-i', 's/old/new/', 'file.txt'])  # Fails on macOS

# GOOD — Python handles OS differences
content = open('file.txt').read()
open('file.txt', 'w').write(content.replace('old', 'new'))

# BAD — Linux-specific CPU count
subprocess.run(['nproc'])

# GOOD — Python is cross-platform
import os
cpu_count = os.cpu_count()
```

### Option 4: Ask before assuming

```
System prompt:
"Before running any OS-specific command (package manager, system paths,
process management), state the OS you're assuming and ask if that's correct.

Example: 'I'll use Homebrew to install this (assuming macOS) — correct?'

If the user confirms the OS once, remember it for the rest of the session
and don't ask again."
```

### Common macOS vs Linux command translations

| Task | Linux | macOS |
|------|-------|-------|
| In-place sed | `sed -i` | `sed -i ''` |
| CPU count | `nproc` | `sysctl -n hw.ncpu` |
| Install package | `apt-get install` | `brew install` |
| Open file | `xdg-open` | `open` |
| Clipboard | `xclip` | `pbcopy/pbpaste` |
| Process info | `/proc/` | `ps`, `sysctl` |
| IP address | `ip addr` | `ifconfig` |

## Expected Token Savings
Debugging OS-specific command failures: ~5,000 tokens
OS in system prompt: prevents all failures

## Environment
- macOS, Windows, or any non-Linux environment
- Source: direct experience, extremely common in cross-platform development
