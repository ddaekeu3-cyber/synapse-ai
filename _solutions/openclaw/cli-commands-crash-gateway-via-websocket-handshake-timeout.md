---
layout: solution
title: "CLI commands crash gateway via WebSocket handshake timeout"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53168
---

# CLI commands crash gateway via WebSocket handshake timeout

## 증상
Crash (process/app exits or hangs)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Avoid all openclaw CLI commands while the gateway is running. Read cron config directly from ~/.openclaw/cron/jobs.json instead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53168
