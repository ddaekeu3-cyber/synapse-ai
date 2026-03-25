---
layout: solution
title: "Skills from plugins registered twice in system prompt, doubling context usage"
category: prompt-engineering
source: https://github.com/anthropics/claude-code/issues/27721
---

# Skills from plugins registered twice in system prompt, doubling context usage

## 증상
Every skill from every enabled plugin appears **twice** in the system-reminder skill list injected into the conversation context. This doubles the token cost of the skill registry and can push sessions over the prompt limit at initialization.

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
Reducing the number of enabled plugins alleviates the issue, but users shouldn't need to disable useful plugins to stay within prompt limits.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/27721
