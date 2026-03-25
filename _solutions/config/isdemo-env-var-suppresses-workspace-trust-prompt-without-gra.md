---
layout: solution
title: "IS_DEMO env var suppresses workspace trust prompt without granting trust, breaking statusline and hooks"
category: config
source: https://github.com/anthropics/claude-code/issues/37780
---

# IS_DEMO env var suppresses workspace trust prompt without granting trust, breaking statusline and hooks

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
`
* Restarting does not help because `IS_DEMO` suppresses the trust prompt again, preventing trust acceptance
* The only fix is to remove `IS_DEMO` from settings, restart, and manually accept the trust prompt

This is the same pattern as #10409, where `--dangerously-skip-permissions` skipped the trust check without granting trust. That issue was fixed, but `IS_DEMO` has the same problem.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37780
