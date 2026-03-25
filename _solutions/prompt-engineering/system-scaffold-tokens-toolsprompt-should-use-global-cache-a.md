---
layout: solution
title: "System scaffold tokens (tools/prompt) should use global cache and not be billed as user input"
category: prompt-engineering
source: https://github.com/anthropics/claude-code/issues/30103
---

# System scaffold tokens (tools/prompt) should use global cache and not be billed as user input

## 증상
Every Claude Code API call includes ~21K tokens of **identical, immutable system scaffolding** — tool definitions (Read, Write, Edit, Bash, Grep, Glob, Agent, WebFetch, etc.) and the system prompt. These tokens:

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
ed system scaffold tokens from the billed input token count. Users should only be billed for tokens they control.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30103
