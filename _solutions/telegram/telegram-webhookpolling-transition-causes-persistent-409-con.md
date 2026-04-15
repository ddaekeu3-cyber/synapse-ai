---
layout: solution
title: "Telegram webhook→polling transition causes persistent 409 conflicts"
category: telegram
source: https://github.com/openclaw/openclaw/issues/20506
description: "Switching Telegram from webhook to polling mode causes continuous 409 Conflict errors. Even after deleting webhook via Telegram API, conflicts persist"
---

# Telegram webhook→polling transition causes persistent 409 conflicts

## 증상
Switching Telegram from webhook to polling mode causes continuous 409 Conflict errors. Even after deleting webhook via Telegram API, conflicts persist until offset file is manually deleted.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
rm ~/.openclaw/telegram/update-offset-default.json
systemctl --user restart openclaw-gateway.service
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20506
