---
layout: solution
title: "[FEATURE] Claude clear context for itself"
category: general
source: https://github.com/anthropics/claude-code/issues/21132
description: "- [x] I have searched existing requests and this feature hasn't been requested"
---

# [FEATURE] Claude clear context for itself

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
giving it the plan prompt + what's been completed with a new context window.
**Giving Claude the ability to prune it's own context window allows it to adhere more closely to plans compared to the autocompact used right now.**

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/21132
