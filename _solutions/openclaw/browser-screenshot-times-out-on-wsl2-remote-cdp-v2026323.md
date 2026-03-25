---
layout: solution
title: "Browser screenshot times out on WSL2 + remote CDP (v2026.3.23)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54148
---

# Browser screenshot times out on WSL2 + remote CDP (v2026.3.23)

## 증상
- **OpenClaw version:** 2026.3.23-2 (7ffe7e4)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using direct CDP WebSocket connection for screenshots, or relying on `browser snapshot` (text mode).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54148
