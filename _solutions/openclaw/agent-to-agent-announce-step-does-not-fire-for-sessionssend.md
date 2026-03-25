---
layout: solution
title: "Agent-to-agent announce step does not fire for sessions_send to ACP-bound sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50487
---

# Agent-to-agent announce step does not fire for sessions_send to ACP-bound sessions

## 증상
When using `sessions_send` to dispatch a task to an ACP-bound session (e.g., Codex or Claude Code with persistent Discord channel bindings), the agent-to-agent announce step never fires after the run completes. The target agent's response is returned to the caller via `sessions_send`, but nothing is posted to the target agent's bound Discord channel.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
The dispatching agent manually cross-posts results to the target channel using the `message` tool after reading the response from `sessions_send` or `sessions_history`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50487
