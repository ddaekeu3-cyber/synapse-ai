---
layout: solution
title: "Task tool subagent processes not terminated after parent session ends (Linux)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/19045
description: "Task tool subagent processes () are not terminated when the parent Claude Code session ends (via crash, Ctrl+C, timeout, or normal exit). These orphaned"
---

# Task tool subagent processes not terminated after parent session ends (Linux)

## 증상
Task tool subagent processes (`claude --resume <session-id>`) are not terminated when the parent Claude Code session ends (via crash, Ctrl+C, timeout, or normal exit). These orphaned processes accumulate over time and consume significant RAM.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
```bash
# Kill all orphaned subagents manually
pkill -f "claude.*--resume"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/19045
