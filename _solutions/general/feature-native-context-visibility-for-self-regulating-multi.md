---
layout: solution
title: "[FEATURE] Native context visibility for self-regulating multi-context workflows"
category: general
source: https://github.com/anthropics/claude-code/issues/18027
description: "- [x] I have searched existing requests and this feature hasn't been requested"
---

# [FEATURE] Native context visibility for self-regulating multi-context workflows

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
- Requires manual setup (statusline hack, skill installation)
- Relies on a hack that could break with updates
- Users must know to invoke the skills
- Not discoverable - users won't find this on their own

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18027
