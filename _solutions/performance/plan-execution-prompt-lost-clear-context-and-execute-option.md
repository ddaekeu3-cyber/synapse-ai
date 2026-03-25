---
layout: solution
title: "Plan execution prompt lost 'clear context and execute' option"
category: performance
source: https://github.com/anthropics/claude-code/issues/38071
---

# Plan execution prompt lost 'clear context and execute' option

## 증상
The plan execution prompt used to offer an option to clear context before executing. This was removed in a recent update, and the prompt now only shows three options:

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
is:

1. Escape the prompt
2. `/clear`
3. Re-invoke the plan

This is clunky and easy to forget, especially when context is already near the limit. The old flow was a single decision point: "yes, and start fresh."

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38071
