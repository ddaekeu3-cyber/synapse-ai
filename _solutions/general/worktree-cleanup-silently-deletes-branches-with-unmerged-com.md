---
layout: solution
title: "Worktree cleanup silently deletes branches with unmerged commits"
category: general
source: https://github.com/anthropics/claude-code/issues/38287
description: "When using to create a worktree session, Claude Code creates a temporary branch (e.g., ). When the session ends, the worktree is cleaned up and the branch"
---

# Worktree cleanup silently deletes branches with unmerged commits

## 증상
When using `claude -w` to create a worktree session, Claude Code creates a temporary branch (e.g., `worktree-expressive-painting-starfish`). When the session ends, the worktree is cleaned up and the branch is deleted — **even if it contains commits that were never pushed or merged**.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Added a `PreToolUse` hook on `ExitWorktree` matcher that checks for unmerged commits and blocks exit:

```json
{
  "hooks": [{ "command": "bash ~/.claude/hooks/worktree-exit-guard.sh", "type": "command" }],
  "matcher": "ExitWorktree"
}
```

This should be built into Claude Code's worktree lifecycle.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38287
