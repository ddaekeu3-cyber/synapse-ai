---
layout: solution
title: "Bash tool: shell-quote mangles { }, heredoc markers, and $ in piped commands"
category: general
source: https://github.com/anthropics/claude-code/issues/32879
description: "Related: #4711 (fixed mangling in v1.0.74). These are further edge cases in the same reconstruction"
---

# Bash tool: shell-quote mangles { }, heredoc markers, and $ in piped commands

## 증상
**Related:** #4711 (fixed `2>&1` mangling in v1.0.74). These are further edge cases in the same `shell-quote` reconstruction path.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Wrap commands in a heredoc so `shell-quote` only sees `bash` and a simple redirect. The heredoc marker must use only `[A-Za-z0-9_]` characters:

```bash
bash <<'BASH_SAFE'
true | true; { echo inside braces; }
BASH_SAFE
```

This can be automated via a PreToolUse hook (we have one at `~/.claude/hooks/bash-preamble.py`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32879
