---
layout: solution
title: "API Streaming Stalls Causing 59-138 Second Delays and Timeouts"
category: config
source: https://github.com/anthropics/claude-code/issues/18028
---

# API Streaming Stalls Causing 59-138 Second Delays and Timeouts

## 증상
- **Claude Code Version:** Latest (npm-local installation)

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Ctrl+C to interrupt the hanging request, then resume. This temporarily works but issue recurs within a few requests, requiring constant manual intervention.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18028
