---
layout: solution
title: "WhatsApp Web listener silently dies after health-monitor stale-socket restart"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49969
---

# WhatsApp Web listener silently dies after health-monitor stale-socket restart

## 증상
The WhatsApp health monitor's `stale-socket` restart cycle reliably re-establishes the **personal/inbound listener** but silently fails to reconnect the **Web listener** (used for all proactive outbound sends). This causes all outbound WhatsApp messaging — cron deliveries, `message` tool sends, announce-mode delivery — to fail with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Full stop → credential wipe → fresh QR scan → start. Lasts 12–30 hours before the cycle repeats.

```bash
openclaw gateway stop
mv ~/.openclaw/credentials/whatsapp/default ~/.openclaw/credentials/whatsapp/default.bak
openclaw channels login --channel whatsapp --account default  # scan QR
openclaw gateway start
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49969
