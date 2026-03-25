---
layout: solution
title: "Heartbeat timer stops after first batch - scheduleNext() timer never re-fires"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31139
---

# Heartbeat timer stops after first batch - scheduleNext() timer never re-fires

## 증상
The heartbeat runner's internal timer fires once after the configured interval, runs all due agents sequentially, but then never re-arms. After the first batch completes, no further heartbeat runs are triggered until the gateway process is restarted.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
External watchdog script that runs `openclaw system event --text "watchdog-heartbeat" --mode now` every 10 minutes from within the container. This bypasses the internal timer entirely and reliably triggers heartbeat runs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31139
