---
layout: solution
title: "Announce delivery timeout hardcoded at 60s, causing timeout failures for complex cron payloads"
category: general
---

# Announce delivery timeout hardcoded at 60s, causing timeout failures for complex cron payloads

## 증상
The `announce` delivery mode for cron jobs has a hardcoded 60-second timeout for generating the announcement message. Complex cron payloads (e.g., daily reports that require multiple tool calls and data aggregation) frequently exceed this timeout, causing the delivery to fail silently.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #31933 참조.

## 해결법
Local patch that raises the timeout cap from 60s to 300s across all 5 affected files:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/31933
