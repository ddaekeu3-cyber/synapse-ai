---
layout: solution
title: "Memory leak: BytesInternalReadableStreamSource ArrayBuffer accumulation (3.3GB in 59s)"
category: general
source: https://github.com/anthropics/claude-code/issues/33589
---

# Memory leak: BytesInternalReadableStreamSource ArrayBuffer accumulation (3.3GB in 59s)

## 증상
Claude Code accumulates ~54 MB/sec of ArrayBuffer memory from the moment it starts, reaching 3.3 GB within 59 seconds on a fresh session. The leak is intermittent — some startups are fine, others immediately balloon.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Restart Claude Code. Unaffected sessions stay at normal memory levels (~400-600 MB).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33589
