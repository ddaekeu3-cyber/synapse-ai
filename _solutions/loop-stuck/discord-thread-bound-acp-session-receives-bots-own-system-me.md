---
layout: solution
title: "Discord thread-bound ACP session receives bot's own ⚙️ system messages, causing infinite turn loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/29325
---

# Discord thread-bound ACP session receives bot's own ⚙️ system messages, causing infinite turn loop

## 증상
When an ACP session is bound to a Discord thread, OpenClaw's own system messages (⚙️ `usage_update`, `available_commands_update`, `session active`, etc.) are forwarded to the ACP session as user prompts. This causes the agent (Codex) to treat them as tasks, generating responses that are again posted to the thread — creating an infinite loop.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Currently mitigating with `acp.stream.coalesceIdleMs: 10000` to reduce flood impact, but this does not prevent the infinite loop itself.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29325
