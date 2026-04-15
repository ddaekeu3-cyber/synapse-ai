---
layout: solution
title: "Multi-account Telegram: bot.on('message') does not fire for group messages on non-default account"
category: telegram
source: https://github.com/openclaw/openclaw/issues/51143
description: "Behavior bug (incorrect output/state without"
---

# Multi-account Telegram: bot.on("message") does not fire for group messages on non-default account

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Route group messages through secondary account with a binding:

{
  bindings: [
    {
      agentId: "main",
      match: {
        channel: "telegram",
        accountId: "maia",
        peer: { kind: "group", id: "-100XXXXXXXXXX" }
      }
    },
    {
      agentId: "other-agent",
      match: { channel: "telegram", accountId: "maia" }
    }
  ]
}

Additional Notes

• mergeTelegramAccountConfig ignores top-level groups for multi-account (isMultiAccount ? void 0 : channelGroups), but resolveTelegramRequireMention reads from cfg.channels.telegram.groups (top level). Docs/code mismatch.
• The 

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51143
