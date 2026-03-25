---
layout: solution
title: "toNormalizedUsage() discards accumulated input/cache tokens — cost tracking underreports ~80% of actual billed usage"
category: general
source: https://github.com/openclaw/openclaw/issues/53734
---

# toNormalizedUsage() discards accumulated input/cache tokens — cost tracking underreports ~80% of actual billed usage

## 증상
`toNormalizedUsage()` in `src/agents/pi-embedded-runner/run.ts` (line ~192) intentionally returns only the **last API call's** `input`, `cacheRead`, and `cacheWrite` tokens instead of the accumulated values from the `UsageAccumulator`. While `output` tokens are correctly accumulated, input/cache tokens are taken from `lastInput`/`lastCacheRead`/`lastCacheWrite`. This causes session transcripts and

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
the context-size display issue (#13698), but it inadvertently broke cost/accounting accuracy.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53734
