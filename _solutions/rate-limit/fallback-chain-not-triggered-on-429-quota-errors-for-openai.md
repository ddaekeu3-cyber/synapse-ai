---
layout: solution
title: "Fallback chain not triggered on 429 quota errors for openai-codex provider"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/24102
---

# Fallback chain not triggered on 429 quota errors for openai-codex provider

## 증상
When an agent hits a 429 "You exceeded your current quota" error from the openai-codex provider, the configured model fallback chain does not trigger. The agent retries the same model repeatedly and fails permanently instead of falling back to the next model in the chain.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
in #687 ("recognize usage limit errors as rate limits for failover") appears to not cover the openai-codex provider's specific 429 quota error message: "You exceeded your current quota, please check your plan and billing details."

Agent config:
{
  "model": {
    "primary": "openai-codex/gpt-5.2-codex",
    "fallbacks": ["kimi-coding/k2p5", "minimax-portal/MiniMax-M2.5"]
  }
}

Per-agent auth via ~/.openclaw/agents/<agentId>/agent/auth-profiles.json (no global auth.order for openai-codex).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24102
