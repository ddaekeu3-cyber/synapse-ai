---
layout: solution
title: "QMD memory_search returns normalized paths that memory_get can't resolve"
category: general
source: https://github.com/openclaw/openclaw/issues/50313
---

# QMD memory_search returns normalized paths that memory_get can't resolve

## 증상
`memory_search` (QMD provider) returns paths with normalized casing and stripped spaces/hyphens, but `memory_get` tries to read the normalized path literally from the filesystem, which fails because the actual files have mixed case and spaces.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
is to use the `read` tool with the manually-corrected path, but that defeats the purpose of the search→get pipeline.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50313
