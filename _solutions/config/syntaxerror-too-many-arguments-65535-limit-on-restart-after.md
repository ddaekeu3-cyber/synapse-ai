---
layout: solution
title: "SyntaxError: Too many arguments (65535 limit) on restart after session crash - Node.js v22.22.0"
category: config
source: https://github.com/anthropics/claude-code/issues/24874
---

# SyntaxError: Too many arguments (65535 limit) on restart after session crash - Node.js v22.22.0

## 증상
- **Claude Code Version:** 2.1.37

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Retry `claude -c` repeatedly. It eventually succeeds, but may take many attempts.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/24874
