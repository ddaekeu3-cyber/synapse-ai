---
layout: solution
title: "Built-in heartbeat timer never fires (requests-in-flight silent skip)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40113
---

# Built-in heartbeat timer never fires (requests-in-flight silent skip)

## 증상
The built-in heartbeat (agents.defaults.heartbeat) logs `heartbeat: started` on gateway start but never fires a tick/poll/skip/complete event. Traced across 3 days (March 6-8, 2026) with versions 2026.3.2 and 2026.3.7.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using a cron job with `sessionTarget: isolated` (runs on CommandLane.Cron) bypasses the contention entirely.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40113
