---
layout: solution
title: "Feature request: MEMORY.md size warning/limit enforcement"
category: memory
source: https://github.com/openclaw/openclaw/issues/45415
---

# Feature request: MEMORY.md size warning/limit enforcement

## 증상
MEMORY.md is silently truncated at ~20K characters. Users have no warning when approaching this limit, leading to lost context.

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
Manually restructure MEMORY.md as a dashboard/index with pointers to detailed files in `memory/`. Use `memory_search` to find content across all files.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45415
