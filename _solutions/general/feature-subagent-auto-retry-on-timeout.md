---
layout: solution
title: "[Feature] Subagent auto-retry on timeout"
category: general
---

# [Feature] Subagent auto-retry on timeout

## 증상
When a subagent times out (via `runTimeoutSeconds`), it simply stops with no retry. For important long-running tasks, this is problematic.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #27874 참조.

## 해결법
)

Currently can wrap `sessions_spawn` with retry logic in agent code, but this is repetitive and error-prone.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/27874
