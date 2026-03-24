---
layout: solution
title: "Gateway crash corrupts session history: thinking blocks become unrecoverable"
category: telegram
---

# Gateway crash corrupts session history: thinking blocks become unrecoverable

## 증상
When the OpenClaw gateway crashes while a session has thinking mode enabled (`thinking=low` or higher), the session becomes permanently unrecoverable. Every subsequent message returns:

에러 메시지:
` on claude-sonnet-4-6)
2. Exchange several messages so thinking blocks accumulate in session history
3. Crash the gateway (e.g. via an invalid config value, unhandled exception, etc.)
4. Gateway rest

## 원인
원본 이슈에서 확인 필요. GitHub Issue #25194 참조.

## 해결법
is `/reset` to discard the entire session history.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/25194
