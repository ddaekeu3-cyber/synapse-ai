---
layout: solution
title: "Model failover treats per-model token exhaustion as provider-level billing failure (Claude Max Plan)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/20316
---

# Model failover treats per-model token exhaustion as provider-level billing failure (Claude Max Plan)

## 증상
When using a Claude Max Plan (subscription with separate per-model token pools), OpenClaw treats a model-specific token exhaustion (e.g., Opus pool depleted) as a provider-level billing failure. This causes the entire `anthropic:default` auth profile to be disabled/cooled down, skipping fallback models (Sonnet, Haiku) that still have available tokens on the same account.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Added a non-Anthropic cloud fallback (Google Gemini Flash) between Anthropic models and local models to ensure a capable model is always available.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20316
