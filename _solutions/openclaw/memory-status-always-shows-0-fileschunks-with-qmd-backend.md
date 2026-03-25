---
layout: solution
title: "memory status always shows 0 files/chunks with qmd backend"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53294
---

# memory status always shows 0 files/chunks with qmd backend

## 증상
When using `qmd` as the memory backend, `memory status` always reports `files: 0, chunks: 0`, even though data exists and `memory_search` works correctly at runtime.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
The actual `memory_search` functionality works fine at runtime since it uses the full `QmdMemoryManager`. The bug only affects the status display.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53294
