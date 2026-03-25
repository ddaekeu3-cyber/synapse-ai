---
layout: solution
title: "Sessions in invisible wait states accumulate for days with no timeout or user notification"
category: performance
source: https://github.com/anthropics/claude-code/issues/25700
---

# Sessions in invisible wait states accumulate for days with no timeout or user notification

## 증상
**Correctness violation**: Orphaned subagents override explicit user denials and continue executing after session exit.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Periodically run: `pkill -f 'claude.*stream-json'` to clear orphaned subagents. Or reboot.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25700
