---
layout: solution
title: "Bug: Telegram topic session stays on previous provider after /model switch during retry loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/46157
---

# Bug: Telegram topic session stays on previous provider after /model switch during retry loop

## 증상
When a Telegram topic session hits Anthropic 429 rate-limit and starts automatic retries, switching the model to `openai-codex/gpt-5.3-codex` does not apply immediately. The session continues sending requests via Anthropic for multiple attempts, causing apparent “hang” and failed switch.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. Run model switch in that exact topic.
2. Immediately run `/new` in same topic.
3. Then send test message.

This clears the stuck queue/context and usually applies new provider correctly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46157
