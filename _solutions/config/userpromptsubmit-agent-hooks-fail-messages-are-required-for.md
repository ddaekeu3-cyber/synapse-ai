---
layout: solution
title: "UserPromptSubmit agent hooks fail: 'Messages are required for agent hooks'"
category: config
source: https://github.com/anthropics/claude-code/issues/26474
---

# UserPromptSubmit agent hooks fail: "Messages are required for agent hooks"

## 증상
Agent-type hooks on `UserPromptSubmit` always fail with the error:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Use `command` type hooks for `UserPromptSubmit` instead of `agent` type.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/26474
