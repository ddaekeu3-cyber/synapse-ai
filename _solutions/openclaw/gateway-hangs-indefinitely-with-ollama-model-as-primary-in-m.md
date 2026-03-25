---
layout: solution
title: "Gateway hangs indefinitely with Ollama model as primary in main Telegram session"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53374
---

# Gateway hangs indefinitely with Ollama model as primary in main Telegram session

## 증상
OpenClaw gateway hangs indefinitely when using a local Ollama model as the primary agent model in a main Telegram session. The gateway's event loop appears to block completely — Telegram polling stalls (no `getUpdates` for 100+ seconds), requiring a full gateway restart to recover.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
in v2026.3.2 (subagent auth inheritance) does not apply here

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53374
