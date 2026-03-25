---
layout: solution
title: "Message tool ignores channel parameter — cross-channel sends broken since 2026.3.22"
category: telegram
source: https://github.com/openclaw/openclaw/issues/54098
---

# Message tool ignores channel parameter — cross-channel sends broken since 2026.3.22

## 증상
The `message` tool ignores the `channel` parameter when sending cross-channel messages. All sends route to the session's native channel (e.g. Telegram) regardless of `channel: "whatsapp"` being explicitly set.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Currently using manual copy-paste for WhatsApp sends from Telegram-originated sessions. Auto-replies (inbound WhatsApp → reply) still work since they use the direct `web-outbound` path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54098
