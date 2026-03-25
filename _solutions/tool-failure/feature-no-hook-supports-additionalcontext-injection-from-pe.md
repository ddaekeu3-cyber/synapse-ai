---
layout: solution
title: "[FEATURE] No hook supports additionalContext injection from permission dialog user input"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/33353
---

# [FEATURE] No hook supports additionalContext injection from permission dialog user input

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
is remembering to submit hashtags only via the main prompt — fragile and unintuitive.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33353
