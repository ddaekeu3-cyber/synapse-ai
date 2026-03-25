---
layout: solution
title: "Discord: health monitor restart loop — post-connection zombie sessions evade circuit breakers"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/38596
---

# Discord: health monitor restart loop — post-connection zombie sessions evade circuit breakers

## 증상
Discord connections successfully complete the handshake (HELLO → READY/RESUMED), run for a period, then become zombie/unstable. The health monitor detects this ("stuck" / "stale-socket") and restarts the provider, but the cycle repeats indefinitely — creating an effective infinite restart loop where **messages sent during unstable periods are silently dropped**.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
it proposed is now shipped, but addresses the HELLO stall case, not this post-connection instability

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38596
