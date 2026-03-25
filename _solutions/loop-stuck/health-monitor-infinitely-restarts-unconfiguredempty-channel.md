---
layout: solution
title: "Health monitor infinitely restarts unconfigured/empty channel plugin (no backoff)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/44398
---

# Health monitor infinitely restarts unconfigured/empty channel plugin (no backoff)

## 증상
- **OpenClaw version**: 2026.3.8

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Remove the `whatsapp` block from `plugins.entries` in openclaw.json and restart the gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44398
