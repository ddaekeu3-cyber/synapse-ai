---
layout: solution
title: "Compaction-written providerOverride in sessions.json bypasses fallback chain on provider rate limit"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/26598
---

# Compaction-written providerOverride in sessions.json bypasses fallback chain on provider rate limit

## 증상
When auto-compaction runs on a session, it writes a `providerOverride` (with

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
must be repeated manually after every compaction
  - There is no config option to disable this behavior

  Real-world impact from this report: a single Google rate-limit spike (caused
  by a momentary overload on gemini-3-pro-preview) cascaded into a full outage
  because Anthropic — a fully functional fallback — was never tried. The
  outage persisted across multiple gateway restarts because the override
  survives restarts. Total recovery time: ~45 minutes across multiple manual
  interventions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26598
