---
layout: solution
title: "Gateway crash-loops on single unresolvable bot token — should gracefully degrade"
category: telegram
source: https://github.com/openclaw/openclaw/issues/42518
description: "Gateway crashes entirely when a single Telegram bot token cannot be resolved from env vars, taking down all other working bots and the entire"
---

# Gateway crash-loops on single unresolvable bot token — should gracefully degrade

## 증상
Gateway crashes entirely when a single Telegram bot token cannot be resolved from env vars, taking down all other working bots and the entire gateway.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
```bash
ln -s ~/.openclaw/secrets/.env ~/.openclaw/.env
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42518
