---
layout: solution
title: "Worktree flag silently hangs when name contains slash"
category: auth
source: https://github.com/anthropics/claude-code/issues/38377
---

# Worktree flag silently hangs when name contains slash

## 증상
When using the `-w` / `--worktree` flag with a name containing `/` (slash), Claude Code silently hangs after completing auth/telemetry. The TUI never launches, no error message is shown, and the process must be killed with Ctrl+C.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Using dashes instead of slashes in the worktree name works fine:

```bash
claude -w 'rodz-open-8749-implement-directory-sync-mapping-with-no-access-groups'
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38377
