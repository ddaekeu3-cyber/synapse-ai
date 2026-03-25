---
layout: solution
title: "WhatsApp provider: stale-socket every ~35 minutes on idle connections (keepalive regression)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34155
---

# WhatsApp provider: stale-socket every ~35 minutes on idle connections (keepalive regression)

## 증상
WhatsApp connections consistently go stale after ~35 minutes of no message traffic, triggering a `health-monitor: restarting (reason: stale-socket)` cycle. This happens with clockwork precision regardless of network conditions.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None — the health-monitor auto-recovery works but doesn't fix the underlying keepalive issue. The connection restores in ~5s each time.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34155
