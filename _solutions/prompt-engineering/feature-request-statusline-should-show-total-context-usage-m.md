---
layout: solution
title: "Feature Request: Statusline should show total context usage (matching /context)"
category: prompt-engineering
source: https://github.com/anthropics/claude-code/issues/15404
---

# Feature Request: Statusline should show total context usage (matching /context)

## 증상
The statusline shows token usage that differs from what `/context` displays, causing confusion about actual context consumption.

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
Run `/context` manually whenever accurate context usage is needed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/15404
