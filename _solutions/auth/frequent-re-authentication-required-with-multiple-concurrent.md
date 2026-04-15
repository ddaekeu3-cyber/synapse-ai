---
layout: solution
title: "Frequent re-authentication required with multiple concurrent Claude Code sessions (OAuth refresh token race condition)"
category: auth
source: https://github.com/anthropics/claude-code/issues/24317
description: "Claude Code requires re-authentication (browser OAuth flow) multiple times per day, even though a valid refresh token exists in . This appears to be"
---

# Frequent re-authentication required with multiple concurrent Claude Code sessions (OAuth refresh token race condition)

## 증상
Claude Code requires re-authentication (browser OAuth flow) multiple times per day, even though a valid refresh token exists in `~/.claude/.credentials.json`. This appears to be caused by a race condition when multiple concurrent Claude Code sessions attempt to refresh the same OAuth token.

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Currently, the only workaround is to close stale Claude Code sessions to reduce the number of concurrent processes competing for token refresh. This is not ideal for users who work across multiple projects simultaneously.

An `ANTHROPIC_API_KEY` environment variable bypasses OAuth entirely but uses API billing instead of the subscription — not a real fix for subscription users.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/24317
