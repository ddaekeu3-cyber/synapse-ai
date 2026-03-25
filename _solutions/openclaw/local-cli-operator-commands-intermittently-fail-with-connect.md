---
layout: solution
title: "Local CLI operator commands intermittently fail with 'connect challenge timeout' / 'handshake timeout' (code 1000/1008) under gateway load"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49726
---

# Local CLI operator commands intermittently fail with "connect challenge timeout" / "handshake timeout" (code 1000/1008) under gateway load

## 증상
Local CLI commands requiring a full authenticated operator WebSocket session (`openclaw devices list`, `openclaw devices approve`, `openclaw nodes status`) intermittently fail with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
~2x memory regression` (#45426) — changed internal chunk structure and memory footprint
- `fix(gateway): bound unanswered client requests` (#45689) — adds new timer pressure on the event loop in the RPC path

Gateway log evidence showing the race (both server and client timeouts firing):
```
2026-03-18T05:08:07.278-04:00 [ws] handshake timeout conn=28e60671... remote=127.0.0.1
2026-03-18T05:08:08.666-04:00 [ws] closed before connect conn=28e60671... code=1000 reason=n/a
2026-03-18T05:08:16.173-04:00 [ws] handshake timeout conn=b5dd94dc... remote=127.0.0.1
2026-03-18T05:08:16.345-04:00 [ws] clo

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49726
