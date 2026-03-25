---
layout: solution
title: "sessions_send from sub-agent flips parent session deliveryContext from Telegram to webchat"
category: telegram
source: https://github.com/openclaw/openclaw/issues/44153
---

# sessions_send from sub-agent flips parent session deliveryContext from Telegram to webchat

## 증상
When a sub-agent replies to the parent agent's main session via `sessions_send`, the parent session's `lastChannel` and `deliveryContext` switch from `telegram` to `webchat`. All subsequent agent replies are delivered to webchat instead of Telegram, effectively dropping them since no webchat client is listening.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Use the `message` tool with explicit `channel: "telegram"` and `target` to send replies, bypassing default delivery context.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44153
