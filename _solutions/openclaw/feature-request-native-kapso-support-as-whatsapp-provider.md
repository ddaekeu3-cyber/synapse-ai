---
layout: solution
title: "Feature Request: Native Kapso support as WhatsApp provider"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53234
---

# Feature Request: Native Kapso support as WhatsApp provider

## 증상
Add native [Kapso](https://kapso.ai) support as a WhatsApp provider in OpenClaw, as an alternative to the current Baileys/QR-based WhatsApp connection.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using a custom webhook server (port 3005) that receives Kapso webhooks and forwards to Telegram. Works but is not seamless.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53234
