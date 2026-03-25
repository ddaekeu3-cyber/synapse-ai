---
layout: solution
title: "Feature: outputLanguage config setting for token savings"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/53541
---

# Feature: outputLanguage config setting for token savings

## 증상
Multilingual users who want structured output (reports, summaries, reviews) in a different language than casual conversation have no config-level way to enforce this. Currently relies on workspace file instructions (SOUL.md, AGENTS.md, MEMORY.md).

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
Multiple workspace files (SOUL.md, AGENTS.md, MEMORY.md) each contain a language rule reminder, which itself costs tokens on every call.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53541
