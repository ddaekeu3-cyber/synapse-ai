---
layout: solution
title: "Cron: manual run stuck after job timeout — stale runningAtMs not cleared"
category: performance
source: https://github.com/openclaw/openclaw/issues/50280
---

# Cron: manual run stuck after job timeout — stale runningAtMs not cleared

## 증상
After a cron job times out (e.g., `Error: cron: job execution timed out`), subsequent manual runs via `cron run` (even with `runMode: force`) are enqueued but never actually start. No new cron session spins up.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Gateway restart (`SIGUSR1`) clears the stale marker and allows runs to proceed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50280
