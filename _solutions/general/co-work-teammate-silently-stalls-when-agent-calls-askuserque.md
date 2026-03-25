---
layout: solution
title: "Co-work teammate silently stalls when agent calls AskUserQuestion"
category: general
source: https://github.com/anthropics/claude-code/issues/29393
---

# Co-work teammate silently stalls when agent calls AskUserQuestion

## 증상
Co-work teammates silently stall indefinitely when the agent calls `AskUserQuestion`. The permission request is emitted but never surfaced in the Desktop UI, leaving the teammate hung with no user-visible indication of what's wrong. The system logs `lam_tool_permission_stalled` after 5 minutes but the user sees nothing.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Users can include explicit instructions in their co-work task prompt:

> "Do not use AskUserQuestion. If you need clarification, use mcp__spaces__reply_to_space to ask me, or make your best judgment and proceed."

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29393
