---
layout: solution
title: "memory_search returns empty for freshly indexed chunks"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41911
---

# memory_search returns empty for freshly indexed chunks

## 증상
After running `openclaw memory index --force` or deleting the SQLite database and rebuilding, `memory search` still returns no results despite chunks being indexed.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `mem0 search` instead of `memory search`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41911
