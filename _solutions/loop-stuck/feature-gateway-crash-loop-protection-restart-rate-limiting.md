---
layout: solution
title: "Feature: Gateway crash loop protection (restart rate limiting)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/16810
---

# Feature: Gateway crash loop protection (restart rate limiting)

## 증상
When the gateway process crashes, there is no rate limiting on restart attempts. On Feb 14 2026, the gateway entered a crash loop with **18 restarts in 5 minutes**, each restart immediately crashing again from the same root cause (invalid Node.js flag `--trace-gc`).

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
was removing the flag. But the meta-problem is that ANY fatal startup error causes unbounded restart attempts with no backoff and no alerting.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/16810
