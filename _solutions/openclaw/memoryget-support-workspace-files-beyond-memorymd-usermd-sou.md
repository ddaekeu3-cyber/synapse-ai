---
layout: solution
title: "memory_get: support workspace files beyond MEMORY.md (USER.md, SOUL.md, etc.)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/12880
---

# memory_get: support workspace files beyond MEMORY.md (USER.md, SOUL.md, etc.)

## 증상
memory_get via /tools/invoke only works for MEMORY.md and memory/*.md. Other workspace files like USER.md, SOUL.md, AGENTS.md return `{disabled: true, error: "path required"}`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `memory_search` with filename as query — works but returns snippets, not full files.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/12880
