---
layout: solution
title: "cli: --worktree silently hangs when name contains a slash"
category: performance
source: https://github.com/anthropics/claude-code/issues/38042
---

# cli: --worktree silently hangs when name contains a slash

## 증상
`claude --worktree <name>` silently hangs (no output, no error) when the worktree name contains a `/` character (e.g., `improve/methodology`). The process never renders the TUI and must be killed manually.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Use `-` or `_` instead of `/` in worktree names:

```bash
claude -w improve-methodology   # works
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38042
