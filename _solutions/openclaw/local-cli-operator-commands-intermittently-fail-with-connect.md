---
layout: solution
title: "Local CLI operator commands intermittently fail with 'connect challenge timeout' / 'handshake timeout' (code 1000/1008) under gateway load"
category: openclaw
---

# Local CLI operator commands intermittently fail with "connect challenge timeout" / "handshake timeout" (code 1000/1008) under gateway load

## 증상
Local CLI commands requiring a full authenticated operator WebSocket session (`openclaw devices list`, `openclaw devices approve`, `openclaw nodes status`) intermittently fail with:



## 원인
원본 이슈에서 확인 필요. GitHub Issue #49726 참조.

## 해결법
, rate limiter setup, origin checks). When the gateway event loop is busy (agent model runs, Discord polling, heartbeats, cron jobs), this synchronous setup block can delay the challenge send past both the 3s server timeout and 2s client timeout.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49726
