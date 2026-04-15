---
layout: solution
title: "Include x-should-retry signal in structured result event output"
category: general
source: https://github.com/anthropics/claude-code/issues/35491
description: "When the Anthropic API returns HTTP 500 with , Claude CLI emits a structured result event"
---

# Include x-should-retry signal in structured result event output

## 증상
When the Anthropic API returns HTTP 500 with `x-should-retry: false`, Claude CLI emits a structured result event like:

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Until this is fixed, wrappers running in `--verbose` mode can parse the debug stderr text for `"not retryable"` or `"x-should-retry": "false"`. But this is fragile and depends on debug output format staying stable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35491
