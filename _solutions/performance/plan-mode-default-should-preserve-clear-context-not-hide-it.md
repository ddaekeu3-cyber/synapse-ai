---
layout: solution
title: "Plan mode: default should preserve clear-context, not hide it"
category: performance
source: https://github.com/anthropics/claude-code/issues/38472
description: "v2.1.75 hid the \"clear context and implement\" option by default when accepting a plan, in response to #25734 / #18523 where users were accidentally"
---

# Plan mode: default should preserve clear-context, not hide it

## 증상
v2.1.75 hid the "clear context and implement" option by default when accepting a plan, in response to #25734 / #18523 where users were accidentally triggering it by pressing Enter too fast.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
punishes intentional users to protect inattentive ones. The destructive default was a real UX problem, but the solution should have been to change the *default selection* (e.g., default to "implement without clearing"), not to hide the option entirely behind an undocumented setting.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38472
