---
layout: solution
title: "Announce delivery timeout hardcoded at 60s, causing timeout failures for complex cron payloads"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/31933
---

# Announce delivery timeout hardcoded at 60s, causing timeout failures for complex cron payloads

## 증상
The `announce` delivery mode for cron jobs has a hardcoded 60-second timeout for generating the announcement message. Complex cron payloads (e.g., daily reports that require multiple tool calls and data aggregation) frequently exceed this timeout, causing the delivery to fail silently.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Local patch that raises the timeout cap from 60s to 300s across all 5 affected files:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31933
