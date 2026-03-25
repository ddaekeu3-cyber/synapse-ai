---
layout: solution
title: "WebSearch tool error: thinking is enabled but reasoning_content is missing"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/32511
---

# WebSearch tool error: thinking is enabled but reasoning_content is missing

## 증상
The `WebSearch` tool consistently returns a 400 error with the following message:

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Using `Bash` tool with `curl` as an alternative works correctly:
```bash
curl -s "https://api.github.com/search/repositories?q=openclaw"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32511
