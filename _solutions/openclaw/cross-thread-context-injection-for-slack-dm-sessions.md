---
layout: solution
title: "Cross-thread context injection for Slack DM sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31125
---

# Cross-thread context injection for Slack DM sessions

## 증상
Each Slack thread creates a separate OpenClaw session with no shared context. When a user makes a directive or decision in one thread and then starts a new thread in the same DM channel, the new session has no awareness of what was just discussed.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Writing important context to workspace files (MEMORY.md, memory/*.md) that get injected into every session. This works for persistent directives but:
- Depends on the agent remembering to write (which it often doesn't)
- Doesn't capture short-term conversational context
- Adds friction to natural conversation flow

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31125
