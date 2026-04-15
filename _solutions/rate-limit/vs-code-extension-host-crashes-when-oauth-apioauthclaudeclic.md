---
layout: solution
title: "VS Code extension host crashes when OAuth /api/oauth/claude_cli/client_data returns 429 — no backoff, kills all extensions"
category: rate-limit
source: https://github.com/anthropics/claude-code/issues/30536
description: "When the Claude Code VS Code extension () receives a HTTP 429 response from the OAuth endpoint , it retries in a tight loop with no exponential backoff."
---

# VS Code extension host crashes when OAuth /api/oauth/claude_cli/client_data returns 429 — no backoff, kills all extensions

## 증상
When the Claude Code VS Code extension (`anthropic.claude-code`) receives a **HTTP 429** response from the OAuth endpoint `https://api.anthropic.com/api/oauth/claude_cli/client_data`, it retries in a tight loop with **no exponential backoff**. This saturates the extension host IPC channel, which then crashes — taking **all other VS Code extensions** (GitHub Copilot Chat, GitHub Pull Request, etc.)

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
None reliable. Signing out + back in temporarily clears the rate limit, but the crash recurs if the limit is hit again. Disabling the Claude Code extension prevents the crash but removes all functionality.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30536
