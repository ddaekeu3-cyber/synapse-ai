---
layout: solution
title: "QMD Memory Index Bug: 0/10 files indexed after 2026.3.23-2 update, memory_search always returns empty"
category: memory
source: https://github.com/openclaw/openclaw/issues/53955
---

# QMD Memory Index Bug: 0/10 files indexed after 2026.3.23-2 update, memory_search always returns empty

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
Use grep-based search instead of `memory_search()`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53955
