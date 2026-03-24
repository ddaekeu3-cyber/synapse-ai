---
layout: solution
title: "Feature: Auto-retry for agent runs on timeout"
category: telegram
---

# Feature: Auto-retry for agent runs on timeout

## 증상
When an agent run times out before generating a response, there's currently no automatic retry mechanism. The user has to manually re-run the request.

에러 메시지:
```
Request timed out before a response was generated. Please try again, or increase agents.defaults.timeoutSeconds in your config.
```

## Proposed Solution

Add an `autoRetry` configuration option u

## 원인
원본 이슈에서 확인 필요. GitHub Issue #32513 참조.

## 해결법
Add an `autoRetry` configuration option under `agents.defaults`:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/32513
