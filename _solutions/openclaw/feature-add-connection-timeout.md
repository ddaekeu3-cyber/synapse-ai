---
layout: solution
title: "[Feature]: Add connection Timeout"
category: openclaw
---

# [Feature]: Add connection Timeout

## 증상
Add a configurable provider connection / first-response timeout for LLM requests.

에러 메시지:
`
- tool timeouts
- other runtime limits

do not affect this behaviour, because they control the overall agent run duration rather than the provider connection / first-response timeout.

As a result, 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #41371 참조.

## 해결법
Introduce a configurable provider connection / first-response timeout for LLM providers.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/41371
