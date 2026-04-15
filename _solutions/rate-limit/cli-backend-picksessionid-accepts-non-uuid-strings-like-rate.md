---
layout: solution
title: "CLI backend: pickSessionId accepts non-UUID strings like 'rate-limited' causing resume failure"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/43288
description: "When a CLI backend (e.g. Claude CLI) hits a rate limit, it returns JSON containing . The function in the dispatch module only checks whether the value is"
---

# CLI backend: pickSessionId accepts non-UUID strings like 'rate-limited' causing resume failure

## 증상
When a CLI backend (e.g. Claude CLI) hits a rate limit, it returns JSON containing `"session_id": "rate-limited"`. The `pickSessionId()` function in the dispatch module only checks whether the value is a non-empty string — it does not validate that the value is actually a usable session identifier (e.g. a UUID). As a result, the literal string `"rate-limited"` gets persisted as the session ID and 

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
Set `sessionMode: "optional"` in the agent's `cliBackend` configuration to prevent OpenClaw from persisting and reusing session IDs altogether. This disables conversation continuity but avoids the crash.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43288
