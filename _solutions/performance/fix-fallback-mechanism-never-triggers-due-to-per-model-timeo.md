---
layout: solution
title: "Fix: Fallback mechanism never triggers due to per-model timeout equaling global run timeout"
category: performance
source: https://github.com/openclaw/openclaw/issues/43400
---

# Fix: Fallback mechanism never triggers due to per-model timeout equaling global run timeout

## 증상
In the current implementation of OpenClaw, the model fallback mechanism fails to trigger when an LLM provider hangs. The agent instead waits for 60 seconds and then completely aborts the run with an `embedded run timeout` error.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
1. Fix the scope issue by replacing `const described = describeFailoverError(normalized);` with `described = describeFailoverError(normalized);`.
2. Adjust the per-model timeout calculation to dynamically divide the remaining time budget among fallback candidates, ensuring it triggers *before* the global timeout. For example:
``javascript
const perModelMs = Math.max(MIN_PER_MODEL_MS, Math.min(25000, Math.floor(45000 / remainingCandidates)));
``
This gives each fallback candidate around 15s to 25s, allowing 2-3 fallbacks to execute successfully within the 60s global run budget.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43400
