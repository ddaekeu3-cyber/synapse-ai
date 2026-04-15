---
layout: solution
title: "Expose session_id and context_window usage to the AI model"
category: context-window
source: https://github.com/anthropics/claude-code/issues/36678
description: "The AI model running inside Claude Code has no way to"
---

# Expose session_id and context_window usage to the AI model

## 증상
The AI model running inside Claude Code has no way to know:

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
1. The statusline script writes context % to a file: `~/.claude/context-usage/{session_id}`
2. The model reads the most recently modified file

This breaks with multiple concurrent sessions because the model doesn't know its own `session_id` to pick the right file.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36678
