---
layout: solution
title: "Nextcloud Talk bot needs --feature response to send messages (undocumented)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53982
---

# Nextcloud Talk bot needs --feature response to send messages (undocumented)

## 증상
When setting up a Nextcloud Talk bot via `occ talk:bot:install`, the `--feature response` flag is required for the bot to send messages back to conversations. Without it, the bot receives webhooks correctly but all replies fail with HTTP 401 ("authentication failed - check bot secret").

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Register the bot with all three features:
```bash
sudo docker exec nextcloud-aio-nextcloud php occ talk:bot:install \
  --feature webhook --feature response --feature reaction \
  "BotName" "<secret>" "<webhook-url>"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53982
