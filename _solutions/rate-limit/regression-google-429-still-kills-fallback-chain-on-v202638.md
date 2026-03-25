---
layout: solution
title: "Regression: Google 429 still kills fallback chain on v2026.3.8 (originally #13623)"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/41492
---

# Regression: Google 429 still kills fallback chain on v2026.3.8 (originally #13623)

## 증상
When multiple models from the same provider (e.g. 3 Google models) are in the failover chain and all return rate-limit errors, the chain does NOT fall through to models from different providers (e.g. DeepSeek at position 4). Instead, it retries the same rate-limited provider and gives up with `FailoverError`.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Interleave providers in the chain so no two adjacent positions share a provider: `deepseek → google → moonshot → google-aistudio → ollama`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41492
