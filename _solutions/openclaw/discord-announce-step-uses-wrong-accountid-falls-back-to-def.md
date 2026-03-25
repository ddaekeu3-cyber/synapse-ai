---
layout: solution
title: "Discord announce step uses wrong accountId (falls back to 'default' instead of session's accountId)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42652
---

# Discord announce step uses wrong accountId (falls back to 'default' instead of session's accountId)

## 증상
When the agent-to-agent announce step delivers messages to Discord, `resolveAnnounceTarget` extracts channel and target from the session key but **does not include accountId**.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Added a token to `channels.discord.accounts.default` (using one bot's token). Announce now works, but all announces appear as that single bot regardless of which agent triggered it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42652
