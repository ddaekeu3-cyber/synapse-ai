---
layout: solution
title: "Anthropic OAuth credentials desync between ~/.claude/.credentials.json and auth-profiles.json — silent subagent failures"
category: auth
source: https://github.com/openclaw/openclaw/issues/44919
---

# Anthropic OAuth credentials desync between ~/.claude/.credentials.json and auth-profiles.json — silent subagent failures

## 증상
- **Version:** OpenClaw 2026.3.11 (29dc654)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
We wrote a systemd timer that syncs `~/.claude/.credentials.json` → all `auth-profiles.json` every 30 minutes and alerts when expiry < 1 hour. Happy to share the script.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44919
