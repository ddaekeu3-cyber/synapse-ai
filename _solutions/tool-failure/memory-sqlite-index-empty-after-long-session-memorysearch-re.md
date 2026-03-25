---
layout: solution
title: "Memory SQLite index empty after long session — memory_search returns 'database is not open'"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/46599
---

# Memory SQLite index empty after long session — memory_search returns 'database is not open'

## 증상
**OpenClaw version:** 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Reading MEMORY.md and daily memory files directly at session start (as specified in AGENTS.md) works as a fallback, but semantic search is unavailable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46599
