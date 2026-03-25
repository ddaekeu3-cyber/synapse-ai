---
layout: solution
title: "systemEvent cron jobs always show skipped/disabled even when enabled"
category: general
source: https://github.com/openclaw/openclaw/issues/45553
---

# systemEvent cron jobs always show skipped/disabled even when enabled

## 증상
systemEvent cron jobs (sessionTarget: main, payload.kind: systemEvent) always show `status: skipped, error: disabled` in run history, even when `enabled: true`.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
None found. Isolated agentTurn crons work fine but cannot call sessions_spawn (needed for worker dispatch). Wake API has no HTTP endpoint (405).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45553
