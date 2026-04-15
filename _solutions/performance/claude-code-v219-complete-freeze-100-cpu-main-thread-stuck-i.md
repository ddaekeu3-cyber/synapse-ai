---
layout: solution
title: "Claude Code v2.1.9 Complete Freeze - 100% CPU, Main Thread Stuck in Infinite Loop (macOS ARM64)"
category: performance
source: https://github.com/anthropics/claude-code/issues/18532
description: "Claude Code v2.1.9 session became completely unresponsive, consuming 100% CPU and ~7GB RAM for nearly 2 hours. The main thread is stuck in an infinite"
---

# Claude Code v2.1.9 Complete Freeze - 100% CPU, Main Thread Stuck in Infinite Loop (macOS ARM64)

## 증상
Claude Code v2.1.9 session became completely unresponsive, consuming **100% CPU** and **~7GB RAM** for nearly **2 hours**. The main thread is stuck in an infinite loop with no progress. This appears to be a continuation of the freeze/hang issues reported in earlier versions.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Force kill the frozen process:
```bash
kill -9 $(pgrep -f "claude" | head -1)
```

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18532
