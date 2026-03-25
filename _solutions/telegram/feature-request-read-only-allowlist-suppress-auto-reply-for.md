---
layout: solution
title: "Feature request: read-only allowlist / suppress auto-reply for WhatsApp DMs"
category: telegram
source: https://github.com/openclaw/openclaw/issues/14076
---

# Feature request: read-only allowlist / suppress auto-reply for WhatsApp DMs

## 증상
When using WhatsApp with `dmPolicy: "allowlist"`, adding a phone number to `allowFrom` makes that contact a **full conversational partner**. Every agent reply in the session is automatically sent back to the WhatsApp sender via the `web-auto-reply` module, even when the agent is primarily interacting on a different channel (e.g., Telegram).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
- Never add third-party numbers to `allowFrom`
- Use `openclaw message send --channel whatsapp --target NUMBER --message TEXT` for outbound-only communication
- Accept that inbound replies from third parties are not received by the agent (must be checked manually on the phone)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/14076
