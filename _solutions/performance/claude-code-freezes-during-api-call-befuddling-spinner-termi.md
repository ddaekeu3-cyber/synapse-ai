---
layout: solution
title: "Claude Code freezes during API call ('Befuddling' spinner), terminal becomes completely unresponsive"
category: performance
source: https://github.com/anthropics/claude-code/issues/24688
---

# Claude Code freezes during API call ("Befuddling" spinner), terminal becomes completely unresponsive

## 증상
Claude Code occasionally freezes mid-execution during the "thinking" phase (shows spinner text like "Befuddling..." or similar). The terminal becomes completely unresponsive — no keyboard input is accepted, including Ctrl-C. The only recovery is force-killing the process (`kill -9`), which destroys the entire session context.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
The only way to recover is `kill -9 <pid>`, which completely destroys the session and all accumulated context. This is not a workaround — it's total data loss. For long-running autonomous sessions with extensive conversation history and in-progress multi-step workflows, this means hours of work and context are irretrievably lost. The session cannot be resumed, checkpointed, or recovered.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/24688
