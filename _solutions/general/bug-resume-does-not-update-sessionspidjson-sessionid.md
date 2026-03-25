---
layout: solution
title: "Bug: /resume does not update sessions/{pid}.json sessionId"
category: general
source: https://github.com/anthropics/claude-code/issues/37737
---

# Bug: /resume does not update sessions/{pid}.json sessionId

## 증상
When using `/resume` inside an already-running Claude Code session to switch to a different conversation, the `~/.claude/sessions/{pid}.json` file is not updated with the new session ID. This causes external tools and hooks to have an incorrect view of which session a process is operating on.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Currently, users must manually `/exit` the original session before resuming it from another window or tool. A `SessionStart` hook can detect and kill conflicting processes, but only for `claude --resume` from the shell — not for `/resume` within an existing session, because the session file is stale.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37737
