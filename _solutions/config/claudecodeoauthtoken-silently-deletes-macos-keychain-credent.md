---
layout: solution
title: "CLAUDE_CODE_OAUTH_TOKEN silently deletes macOS Keychain credentials on exit"
category: config
source: https://github.com/anthropics/claude-code/issues/37512
description: "Setting as an environment variable causes the CLI to silently delete the macOS Keychain credential entry (\"Claude Code-credentials\") on process exit. This"
---

# CLAUDE_CODE_OAUTH_TOKEN silently deletes macOS Keychain credentials on exit

## 증상
Setting `CLAUDE_CODE_OAUTH_TOKEN` as an environment variable causes the CLI to **silently delete the macOS Keychain credential entry** ("Claude Code-credentials") on process exit. This breaks authentication for all other Claude Code sessions (VS Code extension, other terminals).

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
We built a wrapper script that:
1. Extracts the token from `~/.claude/.credentials.json` (not Keychain)
2. Passes `CLAUDE_CODE_OAUTH_TOKEN` only to the child process
3. Restores the Keychain entry after batch completion if deleted

```bash
#!/bin/bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37512
