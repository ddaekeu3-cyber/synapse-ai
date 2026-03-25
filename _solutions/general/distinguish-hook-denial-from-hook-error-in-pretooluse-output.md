---
layout: solution
title: "Distinguish hook denial from hook error in PreToolUse output"
category: general
source: https://github.com/anthropics/claude-code/issues/31592
---

# Distinguish hook denial from hook error in PreToolUse output

## 증상
When a PreToolUse hook intentionally blocks a command (exit code 2), the output is labeled:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
We prefix our stderr messages with `BLOCKED:` to make intent clear, but the outer "hook error" label still appears and causes confusion.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31592
