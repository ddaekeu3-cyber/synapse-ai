---
layout: solution
title: "sessions_send from cron/heartbeat context deadlocks on nested lane (maxConcurrent: 1) - regression from PR #45459"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52271
---

# sessions_send from cron/heartbeat context deadlocks on nested lane (maxConcurrent: 1) - regression from PR #45459

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use `sessions_spawn` (Subagent lane) instead of `sessions_send` (Nested lane), or patch bundled JS per #14214.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52271
