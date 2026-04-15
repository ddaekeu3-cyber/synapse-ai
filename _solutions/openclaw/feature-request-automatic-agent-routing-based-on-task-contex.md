---
layout: solution
title: "Feature Request: Automatic agent routing based on task context"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/32598
description: "When building multi-agent workflows with Claude Code, users currently need to define routing rules manually in and explicitly orchestrate which subagent"
---

# Feature Request: Automatic agent routing based on task context

## 증상
When building multi-agent workflows with Claude Code, users currently need to define routing rules manually in `CLAUDE.md` and explicitly orchestrate which subagent handles which task. This works, but requires upfront configuration and Claude must follow hand-written rules.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manual routing rules in `CLAUDE.md` + explicit Agent tool calls. Works, but fragile and project-specific.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32598
