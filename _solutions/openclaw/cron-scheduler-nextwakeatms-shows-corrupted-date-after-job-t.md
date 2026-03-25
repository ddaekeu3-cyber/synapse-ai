---
layout: solution
title: "Cron scheduler: nextWakeAtMs shows corrupted date after job timeout, jobs stop running"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28403
---

# Cron scheduler: nextWakeAtMs shows corrupted date after job timeout, jobs stop running

## 증상
After a cron job times out or fails, the scheduler appears to get stuck and stops running subsequent scheduled jobs.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Restart gateway
- Manually trigger jobs with `cron run`
- Clear consecutiveErrors and lastStatus in job state

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28403
