---
layout: solution
title: "Telegram media download fails in Docker on Mac (IPv6/IPv4 issue)"
category: docker
source: https://github.com/openclaw/openclaw/issues/43940
description: "Telegram media download fails with when running OpenClaw inside Docker on Mac, even on version 2026.2.26 which supposedly includes the IPv4"
---

# Telegram media download fails in Docker on Mac (IPv6/IPv4 issue)

## 증상
Telegram media download fails with `Failed to download media. Please try again.` when running OpenClaw inside Docker on Mac, even on version 2026.2.26 which supposedly includes the IPv4 fix.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Setting `autoSelectFamily: false` in `channels.telegram.network` resolves the issue:

```json
"channels": {
  "telegram": {
    "network": {
      "autoSelectFamily": false,
      "dnsResultOrder": "ipv4first"
    }
  }
}
```

After applying this setting, Telegram media (voice messages, photos) downloads and processes correctly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43940
