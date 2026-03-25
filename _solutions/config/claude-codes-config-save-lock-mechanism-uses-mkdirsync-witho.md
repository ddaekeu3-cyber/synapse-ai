---
layout: solution
title: "Claude Code's config-save lock mechanism uses mkdirSync without the {recursive: true} flag on Windows."
category: config
source: https://github.com/anthropics/claude-code/issues/33118
---

# Claude Code's config-save lock mechanism uses mkdirSync without the {recursive: true} flag on Windows.

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
(in Claude Code's source) is one character: mkdirSync(path, { recursive: true }) — but that requires Anthropic to patch it. This should be reported at [github.com/anthropics/claude-code/issues](https://github.com/anthropics/claude-code/issues).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33118
