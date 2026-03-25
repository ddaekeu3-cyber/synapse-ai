---
layout: solution
title: "`tail -f | claude -p` hangs forever — official docs example doesn't work (stdin waits for EOF indefinitely)"
category: performance
source: https://github.com/anthropics/claude-code/issues/34455
---

# `tail -f | claude -p` hangs forever — official docs example doesn't work (stdin waits for EOF indefinitely)

## 증상
The official documentation includes this example:

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Use commands that send EOF after completing output:

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34455
