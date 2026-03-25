---
layout: solution
title: "v2026.3.2: Control UI rejects explicit localhost origins in Docker bind=lan unless wildcard/fallback enabled"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33442
---

# v2026.3.2: Control UI rejects explicit localhost origins in Docker bind=lan unless wildcard/fallback enabled

## 증상
After upgrading from `v2026.2.23` to `v2026.3.2`, Control UI websocket connects are rejected with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use both:
- `allowedOrigins` includes `"*"`
- `dangerouslyAllowHostHeaderOriginFallback=true`

This is functional but weaker than desired security posture.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33442
