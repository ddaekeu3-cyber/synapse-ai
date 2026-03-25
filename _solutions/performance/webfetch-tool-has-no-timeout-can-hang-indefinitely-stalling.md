---
layout: solution
title: "WebFetch tool has no timeout — can hang indefinitely, stalling sessions"
category: performance
source: https://github.com/anthropics/claude-code/issues/35426
---

# WebFetch tool has no timeout — can hang indefinitely, stalling sessions

## 증상
The `WebFetch` tool has no timeout parameter. When it encounters a slow or unresponsive URL, it blocks indefinitely with no way for the user to abort the operation.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Use the `context7` MCP server for library documentation instead of WebFetch. For other URLs, warn users before fetching unknown endpoints.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35426
