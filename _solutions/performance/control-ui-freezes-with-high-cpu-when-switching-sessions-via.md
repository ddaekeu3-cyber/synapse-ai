---
layout: solution
title: "Control UI freezes with high CPU when switching sessions via dropdown menu"
category: performance
source: https://github.com/openclaw/openclaw/issues/51685
description: "Crash (process/app exits or"
---

# Control UI freezes with high CPU when switching sessions via dropdown menu

## 증상
Crash (process/app exits or hangs)

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Force refresh: Ctrl + Shift + R
Avoid using dropdown menu for session switching

Additional Context

Issue occurs consistently, 100% reproducible
Browser DevTools Console shows no errors
Browser Task Manager shows high CPU for the tab process

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51685
