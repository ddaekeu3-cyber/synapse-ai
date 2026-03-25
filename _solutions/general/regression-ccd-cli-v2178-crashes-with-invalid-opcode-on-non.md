---
layout: solution
title: "Regression: ccd-cli v2.1.78 crashes with `invalid opcode` on non-AVX CPUs (same as fixed #19907)"
category: general
source: https://github.com/anthropics/claude-code/issues/37277
---

# Regression: ccd-cli v2.1.78 crashes with `invalid opcode` on non-AVX CPUs (same as fixed #19907)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
that was applied after those issues appears to have been lost in v2.1.78.

Previously affected versions: 2.1.14, 2.1.15 (fixed sometime before 2.1.72)
Currently affected: 2.1.78
Root cause: likely a Bun runtime update that reintroduced AVX-dependent instructions

Timeline:
- Before 00:40 (Mar 22, 2026): CLI v2.1.72, everything works
- ~00:40: Claude Desktop restarts, deploys new claude-ssh binary + CLI v2.1.78
- After 00:40: every ccd-cli process crashes with code=-1 after ~10s

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37277
