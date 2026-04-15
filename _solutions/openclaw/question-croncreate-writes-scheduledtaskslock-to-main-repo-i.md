---
layout: solution
title: "[Question] CronCreate writes scheduled_tasks.lock to main repo instead of active worktree"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/32108
description: "When using (via the skill) inside a git worktree, the file is written to the main repository's directory rather than the worktree's directory. This causes"
---

# [Question] CronCreate writes scheduled_tasks.lock to main repo instead of active worktree

## 증상
When using `CronCreate` (via the `/loop` skill) inside a **git worktree**, the `scheduled_tasks.lock` file is written to the **main repository's** `.claude/` directory rather than the worktree's `.claude/` directory. This causes the lock file to show up as an untracked file in the main repo's `git status`.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Adding `**/.claude/scheduled_tasks.lock` to `.gitignore` prevents it from appearing in `git status`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32108
