---
layout: solution
title: "openai-codex provider models rejected as 'Unknown model' in isolated/cron sessions"
category: auth
source: https://github.com/openclaw/openclaw/issues/23677
---

# openai-codex provider models rejected as 'Unknown model' in isolated/cron sessions

## 증상
`openai-codex` provider models (e.g. `openai-codex/gpt-4o`, `openai-codex/gpt-5.1`) cannot be explicitly selected for isolated cron sessions or subagent spawns, even when properly configured in the fallback chain and `agents.defaults.models`.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Using Codex models only as fallbacks in the main session. Cron jobs use Anthropic Haiku directly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23677
