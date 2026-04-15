---
layout: solution
title: "Auto-scroll hides context when permission dialog appears"
category: general
source: https://github.com/anthropics/claude-code/issues/34354
description: "When Claude Code is streaming output and a permission dialog appears (e.g., asking to approve a Bash command), the output continues to auto-scroll. By the"
---

# Auto-scroll hides context when permission dialog appears

## 증상
When Claude Code is streaming output and a permission dialog appears (e.g., asking to approve a Bash command), the output continues to auto-scroll. By the time the user sees the permission prompt, the commentary explaining *why* the tool is being called has scrolled off screen.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Pre-approve commonly used tools via `allowedTools` in settings to reduce the frequency of permission dialogs. But this defeats the purpose of having permission controls for review.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34354
