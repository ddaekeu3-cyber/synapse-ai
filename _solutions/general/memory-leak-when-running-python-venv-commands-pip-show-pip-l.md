---
layout: solution
title: "Memory leak when running Python venv commands (pip show, pip list)"
category: general
source: https://github.com/anthropics/claude-code/issues/38292
description: "Claude Code leaks memory when running Python virtual environment commands like or through the Bash tool. The process eventually"
---

# Memory leak when running Python venv commands (pip show, pip list)

## 증상
Claude Code leaks memory when running Python virtual environment commands like `.venv/bin/pip show` or `.venv/bin/pip list` through the Bash tool. The process eventually OOMs.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Redirect output to a file and read it separately:
```bash
.venv/bin/pip show <package> > /tmp/pip-out.txt 2>&1
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38292
