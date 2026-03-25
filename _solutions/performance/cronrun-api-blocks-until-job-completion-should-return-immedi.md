---
layout: solution
title: "cron.run API blocks until job completion - should return immediately"
category: performance
source: https://github.com/openclaw/openclaw/issues/52898
---

# cron.run API blocks until job completion - should return immediately

## 증상
The `cron.run` API (used to manually trigger cron jobs) waits synchronously for the job to complete before returning a response.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently using `sessions_spawn` instead of `cron.run` for manual triggers, but this bypasses cron scheduling logic and doesn't update cron state properly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52898
