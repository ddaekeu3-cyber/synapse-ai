---
layout: solution
title: "feat: make embedded agent LLM-request timeout configurable (currently hardcoded 15s)"
category: performance
source: https://github.com/openclaw/openclaw/issues/34644
---

# feat: make embedded agent LLM-request timeout configurable (currently hardcoded 15s)

## 증상
The embedded agent fallback uses a hardcoded 15-second timeout for LLM requests. When upstream providers (Anthropic, OpenAI) experience latency spikes, the entire fallback chain times out because each provider gets only 15s.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Currently none — the embedded agent timeout is internal to OpenClaw. Adding more fallback providers helps with availability but doesn't help when all providers are slow simultaneously.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34644
