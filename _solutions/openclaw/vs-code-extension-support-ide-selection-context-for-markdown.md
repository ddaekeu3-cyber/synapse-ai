---
layout: solution
title: "[VS Code Extension] Support IDE selection context for Markdown (.md) files"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37462
---

# [VS Code Extension] Support IDE selection context for Markdown (.md) files

## 증상
The VS Code extension currently passes selected text context (`ide_selection`) only from source code files. Selections made in Markdown (`.md`) files are not recognized or passed to the conversation context.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Manually copy-paste the relevant section into the chat
- Use `@filename.md` to attach the entire file (but this includes unnecessary content)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37462
