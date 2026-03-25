---
layout: solution
title: "Claude defaults to assertion from memory instead of verification from tools"
category: config
source: https://github.com/anthropics/claude-code/issues/32294
---

# Claude defaults to assertion from memory instead of verification from tools

## 증상
Claude Code states facts about schemas, file contents, configurations, and system state from memory/inference rather than checking with available tools. When these assertions are wrong, the error propagates into generated code, SQL, or recommendations — and is reported with the same confidence as verified facts.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
isn't "always run DESCRIBE" — it's "distinguish between 'I know this' and 'I checked this' in reasoning, and default to checking when the cost is low."

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32294
