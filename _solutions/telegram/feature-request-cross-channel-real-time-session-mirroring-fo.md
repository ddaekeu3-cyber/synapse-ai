---
layout: solution
title: "Feature Request: Cross-channel real-time session mirroring for WebChat"
category: telegram
source: https://github.com/openclaw/openclaw/issues/48164
description: "When webchat and Telegram (or any other channel) share the same session (), replies only route to the originating channel. The webchat UI does not receive"
---

# Feature Request: Cross-channel real-time session mirroring for WebChat

## 증상
When webchat and Telegram (or any other channel) share the same session (`agent:main:main`), replies only route to the originating channel. The webchat UI does not receive real-time updates for replies routed via Telegram, and vice versa.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Behavioural: the assistant manually sends a copy to Telegram via `message action=send` after every webchat-originated reply. This is fragile and doesn't work in the other direction (can't push to webchat programmatically).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48164
