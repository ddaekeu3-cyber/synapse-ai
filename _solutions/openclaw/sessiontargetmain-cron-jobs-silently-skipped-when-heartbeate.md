---
layout: solution
title: "sessionTarget='main' cron jobs silently skipped when heartbeat.every='0m'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46046
---

# sessionTarget="main" cron jobs silently skipped when heartbeat.every="0m"

## 증상
`sessionTarget: "main"` cron jobs report `status: "skipped"`, `error: "disabled"` when `agents.defaults.heartbeat.every` is set to `"0m"`, even though the jobs fire at the correct scheduled time.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Set `heartbeat.every` to a non-zero value even when periodic heartbeats are not desired (e.g. `"24h"`). This allows `state.agents` registration to succeed and the interval check to pass, while keeping heartbeat activity minimal.

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46046
