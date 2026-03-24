---
layout: solution
title: "Silent delivery drop after overloaded_error recovery"
category: openclaw
---

# Silent delivery drop after overloaded_error recovery

## 증상
When an Anthropic `overloaded_error` occurs on the first LLM attempt but the session recovers on retry and produces a valid response (`stop=stop`), the Discord message delivery silently fails. The response is recorded in the session transcript but never sent to Discord.

에러 메시지:
`overloaded_error`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49055 참조.

## 해결법
Reduced `auth.cooldowns.billingBackoffHoursByProvider.anthropic` from 0.25h to 0.001h to prevent cascading failures.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49055
