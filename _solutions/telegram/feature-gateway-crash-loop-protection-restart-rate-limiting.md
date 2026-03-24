---
layout: solution
title: "Feature: Gateway crash loop protection (restart rate limiting)"
category: telegram
---

# Feature: Gateway crash loop protection (restart rate limiting)

## 증상
When the gateway process crashes, there is no rate limiting on restart attempts. On Feb 14 2026, the gateway entered a crash loop with **18 restarts in 5 minutes**, each restart immediately crashing again from the same root cause (invalid Node.js flag `--trace-gc`).

에러 메시지:
` flag in NODE_OPTIONS. The fix was removing the flag. But the meta-problem is that ANY fatal startup error causes unbounded restart attempts with no backoff and no alerting.

## Reproduction

1. Add 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #16810 참조.

## 해결법
#### Option A: Built-in restart rate limiting

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/16810
