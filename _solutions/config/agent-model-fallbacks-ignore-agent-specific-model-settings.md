---
layout: solution
title: "Agent model fallbacks ignore agent-specific model settings"
category: config
source: https://github.com/openclaw/openclaw/issues/29200
---

# Agent model fallbacks ignore agent-specific model settings

## 증상
When an agent has a specific model configured (e.g., Codebot with `deepseek/deepseek-chat-v3-0324`), the agent falls back to the global fallback chain (defined in `agents.defaults.model.fallbacks`) instead of using agent-specific fallbacks when the primary model hits a rate limit.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Added the model to the agent's `models.json` file so it's recognized, but the fallback behavior is still incorrect.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29200
