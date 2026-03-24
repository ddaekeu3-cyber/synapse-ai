---
layout: solution
title: "The OpenClaw Control UI incorrectly handles provider prefixes"
category: gog
---

# The OpenClaw Control UI incorrectly handles provider prefixes

## 증상
Regression (worked before, now fails)

에러 메시지:
`GatewayRequestError: model not allowed`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52482 참조.

## 해결법
es when attempting to switch to local models (e.g., Ollama) via the model dropdown, resulting in a `GatewayRequestError: model not allowed` exception.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52482
