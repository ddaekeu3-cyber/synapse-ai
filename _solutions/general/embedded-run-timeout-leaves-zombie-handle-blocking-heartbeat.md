---
layout: solution
title: "Embedded run timeout leaves zombie handle blocking heartbeat delivery"
category: general
---

# Embedded run timeout leaves zombie handle blocking heartbeat delivery

## 증상
When an embedded run times out but the underlying provider promise never settles (e.g., dead HTTP connection, hung stream), the run handle stays in `ACTIVE_EMBEDDED_RUNS` permanently. This silently kills all subsequent heartbeat deliveries for the session.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52231 참조.

## 해결법
After the abort timer fires and a grace period elapses (e.g., 30-60s), forcibly remove the handle:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52231
