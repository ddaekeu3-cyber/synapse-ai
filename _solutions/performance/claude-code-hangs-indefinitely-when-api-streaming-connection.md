---
layout: solution
title: "Claude Code hangs indefinitely when API streaming connection stalls (no read timeout)"
category: performance
source: https://github.com/anthropics/claude-code/issues/25979
---

# Claude Code hangs indefinitely when API streaming connection stalls (no read timeout)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
External watchdog daemon that monitors JSONL session files and kills processes with no writes for >5 minutes. Available at user's request.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25979
