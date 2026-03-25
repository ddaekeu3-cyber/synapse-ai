---
layout: solution
title: "CLI WebSocket handshake timeout on Windows (intermittent, ~80% failure rate)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48736
---

# CLI WebSocket handshake timeout on Windows (intermittent, ~80% failure rate)

## 증상
- **OpenClaw:** 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
A standalone helper script implementing the v2 handshake protocol directly works 100% reliably. Happy to share if helpful for debugging.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48736
