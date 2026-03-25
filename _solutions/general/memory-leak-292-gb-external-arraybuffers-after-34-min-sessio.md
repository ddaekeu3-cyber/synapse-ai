---
layout: solution
title: "Memory leak: 2.92 GB external ArrayBuffers after 34 min session (v2.1.72)"
category: general
source: https://github.com/anthropics/claude-code/issues/33320
---

# Memory leak: 2.92 GB external ArrayBuffers after 34 min session (v2.1.72)

## 증상
Claude Code leaks ~490 MB/hour of external (native) ArrayBuffer memory. A 34-minute session accumulated 3.39 GB of external memory (2.92 GB in ArrayBuffers), while the V8 heap itself remained healthy at ~126 MB.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Restart Claude Code sessions every 1-2 hours to reclaim memory.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33320
