---
layout: solution
title: "v2.1.81: VS Code extension fails with 503 error on Bedrock (CLI works fine)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38134
---

# v2.1.81: VS Code extension fails with 503 error on Bedrock (CLI works fine)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Rolling back to v2.1.79 resolves the issue.

  Notes

  - Claude Code CLI works fine with same credentials
  - No authentication errors (401/403), specifically a 503 service error

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38134
