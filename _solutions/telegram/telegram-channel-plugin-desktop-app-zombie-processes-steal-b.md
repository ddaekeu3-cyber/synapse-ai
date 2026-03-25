---
layout: solution
title: "Telegram channel plugin: Desktop app zombie processes steal bot polling, MCP server fails"
category: telegram
source: https://github.com/anthropics/claude-code/issues/37624
---

# Telegram channel plugin: Desktop app zombie processes steal bot polling, MCP server fails

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Fully quit the Desktop app (not just close the window) and kill any zombie Claude processes before starting the CLI with `--channels`. However, this defeats the purpose for users who want to use both the Desktop app for chat and the CLI for Telegram integration.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37624
