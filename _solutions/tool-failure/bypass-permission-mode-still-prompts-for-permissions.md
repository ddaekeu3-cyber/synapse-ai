---
layout: solution
title: "Bypass permission mode still prompts for permissions"
category: tool-failure
source: https://github.com/anthropics/claude-code/issues/37653
---

# Bypass permission mode still prompts for permissions

## 증상
When selecting "bypass" permission mode at session start, Claude Code still prompts for permission on tool calls. This defeats the purpose of the bypass option.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Manually adding wildcard allow rules (`Bash(*)`, `Read(*)`, etc.) to `~/.claude/settings.local.json` (global) and per-project `settings.local.json` files.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37653
