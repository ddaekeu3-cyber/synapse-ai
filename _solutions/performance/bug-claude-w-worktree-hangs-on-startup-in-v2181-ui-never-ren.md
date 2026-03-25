---
layout: solution
title: "Bug: `claude -w <worktree>` hangs on startup in v2.1.81, UI never renders"
category: performance
source: https://github.com/anthropics/claude-code/issues/37874
---

# Bug: `claude -w <worktree>` hangs on startup in v2.1.81, UI never renders

## 증상
`claude -w <worktree-name>` freezes on startup in v2.1.81 — the interactive UI never renders. The process starts and establishes TCP connections, then hangs indefinitely. Running `claude` directly inside the worktree directory works fine. Downgrading to v2.1.80 resolves the issue immediately.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
```bash
# Instead of:
claude -w my-worktree-name

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37874
