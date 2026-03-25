---
layout: solution
title: "Bug: Heartbeat tool_use blocks leak into parent session transcript, corrupting Anthropic sessions"
category: general
source: https://github.com/openclaw/openclaw/issues/41329
---

# Bug: Heartbeat tool_use blocks leak into parent session transcript, corrupting Anthropic sessions

## 증상
When a heartbeat agent runs on a non-Anthropic model (e.g. Grok 4.1 Fast via OpenRouter) and produces an alert that gets delivered to the parent session channel, the heartbeat's `tool_use` blocks leak into the parent session's transcript **without their corresponding `tool_result` blocks**. The next Anthropic API call for that session then fails with:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Switching the heartbeat model to an Anthropic model (e.g. `anthropic/claude-haiku-4-5`) appears to resolve the issue, likely because Anthropic's transcript hygiene includes tool result pairing repair.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41329
