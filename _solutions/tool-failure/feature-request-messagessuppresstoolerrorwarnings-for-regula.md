---
layout: solution
title: "Feature Request: messages.suppressToolErrorWarnings for regular sessions (not just heartbeat)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/20284
---

# Feature Request: messages.suppressToolErrorWarnings for regular sessions (not just heartbeat)

## 증상
Currently, `messages.suppressToolErrors: true` suppresses non-mutating tool errors but **not** mutating tool errors (like `exec`, `sessions_send`, `write`, etc.). The check in `shouldShowToolErrorWarning` always returns `true` for mutating tools, bypassing the suppress flag:

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Currently handling this via SOUL.md rules (telling the agent to respond gracefully when `sessions_send` fails), but the platform still injects the error message as a separate chat message before the agent reply — so the workaround is incomplete.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20284
