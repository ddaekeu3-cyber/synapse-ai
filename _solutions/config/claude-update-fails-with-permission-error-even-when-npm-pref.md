---
layout: solution
title: "claude update fails with permission error even when npm prefix is user-owned"
category: config
source: https://github.com/anthropics/claude-code/issues/37753
---

# claude update fails with permission error even when npm prefix is user-owned

## 증상
`2.1.77` (attempting to update to `2.1.81`)

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Running `npm install -g @anthropic-ai/claude-code` directly works without any permission issues.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37753
