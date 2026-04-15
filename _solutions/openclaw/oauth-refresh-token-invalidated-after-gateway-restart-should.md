---
layout: solution
title: "OAuth refresh token invalidated after gateway restart — should auto-recover"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46385
description: "When the OpenClaw gateway restarts (e.g., during or ), the OpenAI Codex OAuth refresh token can become invalidated. The restart interrupts an in-flight"
---

# OAuth refresh token invalidated after gateway restart — should auto-recover

## 증상
When the OpenClaw gateway restarts (e.g., during `openclaw update run` or `config.patch`), the OpenAI Codex OAuth refresh token can become invalidated. The restart interrupts an in-flight token refresh, consuming the single-use refresh token without storing the new one. Subsequent requests fail with:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
- Manual re-auth after every gateway restart that coincides with a token refresh
- Running a "keep-alive" cron job before other crons to force a clean refresh in isolation
- Setting `agents.defaults.model.fallbacks` to prevent expensive primary model burn on auth failures

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46385
