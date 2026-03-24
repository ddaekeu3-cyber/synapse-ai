---
layout: solution
title: "Two-Strike Enforcement for Sub-Agent Error Handling"
category: general
---

# Two-Strike Enforcement for Sub-Agent Error Handling

## 증상
Sub-agent sessions may encounter repeated non-transient errors but continue running indefinitely, consuming resources and potentially causing cascading failures. There is no automated mechanism to terminate or restart misbehaving sessions.

에러 메시지:
`error_count`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52703 참조.

## 해결법
Two-Strike Enforcement

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52703
