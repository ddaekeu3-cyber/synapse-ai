---
layout: solution
title: "Feature request: Include model name in agent/embedded error logs"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/41301
---

# Feature request: Include model name in agent/embedded error logs

## 증상
When an API error occurs (e.g., rate limit, overload), the error log currently only shows:

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Manually grep logs for the runId and look at preceding lines to identify the active provider.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41301
