---
layout: solution
title: "Feishu extension silently fails on Lark international (domain: 'lark') — WebSocket not supported by platform, no fallback or user-facing error"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/51663
---

# Feishu extension silently fails on Lark international (domain: "lark") — WebSocket not supported by platform, no fallback or user-facing error

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #51663에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
In monitor.ts, detect `domain === "lark"` (or the larksuite.com domain) and:
- Force `connectionMode: "webhook"` when domain is lark, or
- Throw a clear configuration error early if websocket is requested with domain lark

I have a working patch for this running in production on Lark international 
(v2026.2.9 baseline). Happy to open a PR once the direction is confirmed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51663
