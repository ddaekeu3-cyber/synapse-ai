---
layout: solution
title: "Feature: trustedWebhooks for Discord — allow specific webhooks to trigger agent responses"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42663
description: "OpenClaw drops all Discord webhook messages because and defaults to . This prevents users from building voice input pipelines, iOS Shortcuts, or external"
---

# Feature: trustedWebhooks for Discord — allow specific webhooks to trigger agent responses

## 증상
OpenClaw drops all Discord webhook messages because `author.bot === true` and `allowBots` defaults to `false`. This prevents users from building voice input pipelines, iOS Shortcuts, or external automations that send messages via Discord webhooks and expect the agent to respond.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
would check trusted webhooks before dropping:

```javascript
if (author.bot) {
    const webhookId = resolveDiscordWebhookId(message);
    const trusted = webhookId ? resolveTrustedWebhook(discordConfig, guildId, webhookId) : null;
    
    if (trusted) {
        if (trusted.mapToUser) sender.id = trusted.mapToUser;
        logVerbose(`discord: trusted webhook ${webhookId}`);
    } else if (!allowBots && !sender.isPluralKit) {
        logVerbose("discord: drop bot message (allowBots=false)");
        return null;
    }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42663
