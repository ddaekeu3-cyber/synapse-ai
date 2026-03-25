---
layout: solution
title: "Bug: Bash tool does not stream live terminal output"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/34732
---

# Bug: Bash tool does not stream live terminal output

## 증상
When Claude Code runs shell commands via the Bash tool inside the VS Code extension, all output is captured silently and only shown after the command completes. The user sees nothing until the command finishes — which can be many minutes or longer.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
User must copy-paste commands and run them manually in their own terminal, which defeats the purpose of an AI coding assistant.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34732
