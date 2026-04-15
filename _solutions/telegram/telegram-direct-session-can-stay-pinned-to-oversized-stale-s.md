---
layout: solution
title: "Telegram direct session can stay pinned to oversized stale session; /new does not reliably cut over"
category: telegram
source: https://github.com/openclaw/openclaw/issues/50465
description: "Telegram direct chats can get stuck showing with no final reply when the direct-session mapping remains attached to a very large stale session. In this"
---

# Telegram direct session can stay pinned to oversized stale session; /new does not reliably cut over

## 증상
Telegram direct chats can get stuck showing `typing...` with no final reply when the direct-session mapping remains attached to a very large stale session. In this state, Telegram transport may still be healthy and direct Bot API sends can still work. Running `/new` in Telegram did not reliably move the direct chat onto a fresh session. Manually removing the direct-session mapping and restarting t

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
1. Back up `~/.openclaw/agents/main/sessions/sessions.json`
2. Remove the `agent:main:telegram:direct:<chat_id>` key
3. Restart gateway
4. Send a fresh Telegram message

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50465
