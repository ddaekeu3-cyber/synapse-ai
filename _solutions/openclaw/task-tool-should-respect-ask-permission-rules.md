---
layout: solution
title: "Task tool should respect `ask` permission rules"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/29333
description: "The Task tool has \"Permission Required: No\" in Claude Code, which means rules in are silently ignored. works (documented for disabling sub-agents), but"
---

# Task tool should respect `ask` permission rules

## 증상
The Task tool has "Permission Required: No" in Claude Code, which means `ask` rules in `permissions` are silently ignored. `deny` works (documented for disabling sub-agents), but there is no way to require user approval before a sub-agent spawns.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
The delegate's first action is now a Bash `echo` showing its task summary. A `Bash(echo "🔓 LOCKBOX DELEGATE:*")` entry in `ask` triggers a real permission prompt (since Bash does respect `ask` rules, and `ask` is evaluated before `allow`). This works but is a workaround — the approval happens after the sub-agent spawns rather than before.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29333
