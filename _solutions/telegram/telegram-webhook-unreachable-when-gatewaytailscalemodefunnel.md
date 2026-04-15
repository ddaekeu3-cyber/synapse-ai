---
layout: solution
title: "Telegram webhook unreachable when gateway.tailscale.mode=funnel (port mismatch)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/30022
description: "When is enabled alongside Telegram webhook mode ( set), inbound webhook requests from Telegram fail with 405 Method Not"
---

# Telegram webhook unreachable when gateway.tailscale.mode=funnel (port mismatch)

## 증상
When `gateway.tailscale.mode: "funnel"` is enabled alongside Telegram webhook mode (`channels.telegram.webhookUrl` set), inbound webhook requests from Telegram fail with **405 Method Not Allowed**.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Disable auto-managed funnel and manually configure Tailscale Funnel to the webhook port:

```bash
openclaw config set gateway.tailscale.mode off
tailscale funnel --bg 8787
openclaw gateway restart
```

This survives gateway restarts since `tailscale.mode: "off"` prevents the gateway from overriding the funnel target on startup.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30022
