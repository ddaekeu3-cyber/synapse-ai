---
layout: solution
title: "Feature Request: Built-in Peer Model Audit Gate for Code Quality Enforcement"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/34535
---

# Feature Request: Built-in Peer Model Audit Gate for Code Quality Enforcement

## 증상
I've been running a production workflow where GPT (acting as an independent auditor) reviews Claude Code's output before any work can be marked complete. After several months of iteration, this cross-model audit loop has proven significantly more effective than prompt-based instructions alone. I'd like to propose this as a native Claude Code feature.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Cross-Model Audit Gate (Working Implementation)

I've implemented this as a Claude Code hook workflow:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34535
