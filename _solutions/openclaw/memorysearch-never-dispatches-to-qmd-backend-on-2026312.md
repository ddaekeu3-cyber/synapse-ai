---
layout: solution
title: "memory_search never dispatches to QMD backend on 2026.3.12"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49351
---

# memory_search never dispatches to QMD backend on 2026.3.12

## 증상
`memory.backend: "qmd"` is configured correctly and the gateway logs `qmd memory startup initialization armed for agent "main"` at startup, but `memory_search` tool calls never actually dispatch to QMD. The gateway silently falls back to the builtin backend.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `memory.backend: "builtin"` with `memorySearch.query.hybrid` configuration works correctly for BM25 + vector search.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49351
