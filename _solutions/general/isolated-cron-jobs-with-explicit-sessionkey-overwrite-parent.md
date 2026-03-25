---
layout: solution
title: "Isolated cron jobs with explicit sessionKey overwrite parent session's updatedAt, preventing daily reset"
category: general
source: https://github.com/openclaw/openclaw/issues/51000
---

# Isolated cron jobs with explicit sessionKey overwrite parent session's updatedAt, preventing daily reset

## 증상
Isolated cron jobs (`sessionTarget: "isolated"`) that specify a `sessionKey` matching the main session key (e.g., `agent:main:main`) overwrite the parent session entry in the session store, including `updatedAt`. This prevents the daily session reset from triggering.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Set each isolated cron's `sessionKey` to a unique value (e.g., `agent:main:cron:<job-name>`) instead of `agent:main:main`. This prevents the cron from writing to the main session's store entry.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51000
