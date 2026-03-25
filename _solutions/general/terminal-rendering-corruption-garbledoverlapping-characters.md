---
layout: solution
title: "Terminal rendering corruption (garbled/overlapping characters) in v2.1.83"
category: general
source: https://github.com/anthropics/claude-code/issues/38578
---

# Terminal rendering corruption (garbled/overlapping characters) in v2.1.83

## 증상
After updating from v2.1.81 to v2.1.83, intermittent terminal rendering corruption appears where characters overlap and become garbled/unreadable.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
in v2.1.83: *"Fixed mouse tracking escape sequences leaking to shell prompt after exit"* — escape sequence handling changes can cause terminal rendering corruption.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38578
