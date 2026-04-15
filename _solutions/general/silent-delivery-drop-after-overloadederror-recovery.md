---
layout: solution
title: "Silent delivery drop after overloaded_error recovery"
category: general
source: https://github.com/openclaw/openclaw/issues/49055
description: "When an Anthropic occurs on the first LLM attempt but the session recovers on retry and produces a valid response (), the Discord message delivery"
---

# Silent delivery drop after overloaded_error recovery

## 증상
When an Anthropic `overloaded_error` occurs on the first LLM attempt but the session recovers on retry and produces a valid response (`stop=stop`), the Discord message delivery silently fails. The response is recorded in the session transcript but never sent to Discord.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Reduced `auth.cooldowns.billingBackoffHoursByProvider.anthropic` from 0.25h to 0.001h to prevent cascading failures.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49055
