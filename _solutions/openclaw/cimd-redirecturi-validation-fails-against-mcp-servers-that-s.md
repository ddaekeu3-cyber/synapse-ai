---
layout: solution
title: "CIMD redirect_uri validation fails against MCP servers that support DCR (v2.1.81)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38296
---

# CIMD redirect_uri validation fails against MCP servers that support DCR (v2.1.81)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Downgrade to Claude Code 2.1.80: claude update --version 2.1.80

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38296
