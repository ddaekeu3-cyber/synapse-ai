---
layout: solution
title: "ACP/acpx: claude-agent-acp requires API key, doesn't work with Claude Max subscription OAuth tokens"
category: auth
source: https://github.com/openclaw/openclaw/issues/53456
---

# ACP/acpx: claude-agent-acp requires API key, doesn't work with Claude Max subscription OAuth tokens

## 증상
The stock acpx plugin in v2026.3.22+ uses `@zed-industries/claude-agent-acp` as the ACP adapter for Claude Code sessions. This adapter requires a standard Anthropic API key (`sk-ant-api03-...`) and can't use Claude Max subscriptions with the adapter.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently none for Max subscription users without a separate API key. Subagent spawning (`runtime="subagent"`) works as an alternative for non-ACP coding tasks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53456
