---
layout: solution
title: "Model failover: cooldown blocks different models on same provider (per-profile vs per-model)"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/44332
---

# Model failover: cooldown blocks different models on same provider (per-profile vs per-model)

## 증상
When a specific model (e.g. `anthropic/claude-sonnet-4-6`) hits a rate limit on Claude Max, the cooldown is applied to the **auth profile** (provider-level), not the specific model. This prevents automatic failover to a different model on the same provider (e.g. `anthropic/claude-opus-4-6`) even though that model is available and not rate-limited.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Manually switching with `/model anthropic/claude-opus-4-6` works because user overrides bypass cooldown logic.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44332
