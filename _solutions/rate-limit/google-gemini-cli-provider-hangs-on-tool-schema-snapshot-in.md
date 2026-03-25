---
layout: solution
title: "google-gemini-cli provider hangs on tool schema snapshot in subagent/cron (isolated sessions) — misreported as rate limit"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/19205
---

# google-gemini-cli provider hangs on tool schema snapshot in subagent/cron (isolated sessions) — misreported as rate limit

## 증상
`google-gemini-cli` provider times out on tool schema snapshot when running in subagent or isolated cron sessions. The same provider works fine in the persistent main session. The error is misreported as "rate limit" in logs.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Switch to a direct Gemini API key (`GEMINI_API_KEY` via OpenRouter or AI Studio) for subagent/cron contexts. The `google-gemini-cli` OAuth provider works only in persistent main sessions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19205
