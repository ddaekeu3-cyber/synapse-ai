---
layout: solution
title: "Telegram channel fails with 404 - $VARIABLE references in botToken not resolved (v2026.3.2)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/34247
---

# Telegram channel fails with 404 - $VARIABLE references in botToken not resolved (v2026.3.2)

## 증상
Telegram channel fails to start with repeated `404: Not Found` errors on all Bot API methods (`deleteWebhook`, `setMyCommands`, `deleteMyCommands`, `sendMessage`). The channel enters a retry loop (up to 10/10) and never recovers.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Replace the variable reference with the literal token:

```bash
openclaw config set channels.telegram.botToken "ACTUAL_TOKEN_HERE"
```

After this, gateway hot-reloads and `openclaw doctor` reports `Telegram: ok`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34247
