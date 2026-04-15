---
layout: solution
title: "Memory leak causing V8 OOM crashes (SIGABRT) on extended sessions"
category: general
source: https://github.com/anthropics/claude-code/issues/18011
description: "Claude Code sessions are crashing due to V8 heap exhaustion (Out of Memory). The Node.js process accumulates memory until garbage collection fails,"
---

# Memory leak causing V8 OOM crashes (SIGABRT) on extended sessions

## 증상
Claude Code sessions are crashing due to V8 heap exhaustion (Out of Memory). The Node.js process accumulates memory until garbage collection fails, triggering `abort()`.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Increasing Node.js heap size delays the crash:
```bash
export NODE_OPTIONS="--max-old-space-size=8192"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18011
