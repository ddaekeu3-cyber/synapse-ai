---
layout: solution
title: "Heartbeat runner never re-fires when a run exceeds the 'every' interval"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39173
---

# Heartbeat runner never re-fires when a run exceeds the 'every' interval

## 증상
When a heartbeat run takes longer than (or close to) the configured `every` interval, the next heartbeat never fires. The runner becomes permanently stuck until the gateway is restarted.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Increase `every` to a value safely above the maximum expected heartbeat duration (e.g. `60m`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39173
