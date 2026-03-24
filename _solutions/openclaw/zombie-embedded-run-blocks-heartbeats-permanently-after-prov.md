---
layout: solution
title: "Zombie embedded run blocks heartbeats permanently after provider timeout"
category: openclaw
---

# Zombie embedded run blocks heartbeats permanently after provider timeout

## 증상
When an embedded run times out but the underlying provider promise never settles (e.g., dead connection, hung HTTP stream), the run handle stays in `ACTIVE_EMBEDDED_RUNS` permanently. Since `resolveActiveRunQueueAction` returns `"drop"` for heartbeats when `isActive === true`, all subsequent heartbe



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52224 참조.

## 해결법
After the abort timer fires and a grace period elapses (e.g., 30-60s), forcibly remove the handle from `ACTIVE_EMBEDDED_RUNS` regardless of whether the promise settled:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52224
