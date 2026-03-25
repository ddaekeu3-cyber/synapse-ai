---
layout: solution
title: "Cron systemEvent job times out after ~960s even though agent runs in main session"
category: performance
source: https://github.com/openclaw/openclaw/issues/50621
---

# Cron systemEvent job times out after ~960s even though agent runs in main session

## 증상
When a cron job is configured with `sessionTarget: "main"` and `payload.kind: "systemEvent"`, the cron scheduler enforces a timeout on the agent turn. If the turn takes too long, the job is marked as `error: "cron: job execution timed out"`.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
- For `systemEvent` jobs targeting `main`, either remove the timeout entirely or make it configurable (similar to `timeoutSeconds` that exists for `agentTurn` payloads).
- Alternatively, document what the timeout is and how to work around it for long-running tasks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50621
