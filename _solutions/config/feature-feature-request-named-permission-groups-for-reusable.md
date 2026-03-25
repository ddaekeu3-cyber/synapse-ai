---
layout: solution
title: "[FEATURE] Feature request: named permission groups for reusable rule bundles"
category: config
source: https://github.com/anthropics/claude-code/issues/32451
---

# [FEATURE] Feature request: named permission groups for reusable rule bundles

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
A `PreToolUse` hook script can approximate this by maintaining rule bundles in an external config and returning approve/deny decisions. This works but is fragile, undiscoverable, and doesn't integrate with the settings UI or "always allow" flow.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32451
