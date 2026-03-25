---
layout: solution
title: "overloaded_error from Anthropic API does not trigger model fallback"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49696
---

# overloaded_error from Anthropic API does not trigger model fallback

## 증상
When Anthropic returns an `overloaded_error` during streaming, OpenClaw does **not** trigger model fallback to the next model in `agents.defaults.model.fallbacks`. The agent run silently fails with `isError=true` and the user receives no response.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently. The only mitigation is to manually switch models via `/model sonnet` when Opus is overloaded, but that requires the user to already know what happened.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49696
