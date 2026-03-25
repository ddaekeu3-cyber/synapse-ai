---
layout: solution
title: "Memory leak: Claude process using 304GB+ memory"
category: performance
source: https://github.com/anthropics/claude-code/issues/17615
---

# Memory leak: Claude process using 304GB+ memory

## 증상
<img width="891" height="67" alt="Image" src="https://github.com/user-attachments/assets/319fc0c5-348a-4d90-9ef5-906fad155835" />

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
```bash
pkill -9 claude
```

## Additional Context
This occurred during a code refactoring session involving multiple file reads, edits, and grep operations across a Go backend codebase (~33 modules).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/17615
