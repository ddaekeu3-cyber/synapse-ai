---
layout: solution
title: "Feature Request: Split gateway daemon and agent runtime for container isolation"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/27259
---

# Feature Request: Split gateway daemon and agent runtime for container isolation

## 증상
Currently, the OpenClaw gateway daemon is a single Node.js process that handles everything: messaging (Telegram/Discord/etc.), node WebSocket connections, agent sessions, exec tool execution, approvals management, and configuration. Agent tool calls (`exec`, `nodes.run`, `Read`, `Edit`) are internal function calls within the same process.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
The audit-based model: monitor agent sessions for suspicious allowlist modifications, use a separate monitoring agent, and accept that the allowlist prevents accidental damage rather than determined exploitation. This works but relies on detection rather than prevention.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27259
