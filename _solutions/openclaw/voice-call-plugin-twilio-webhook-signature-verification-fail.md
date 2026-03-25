---
layout: solution
title: "Voice-call plugin: Twilio webhook signature verification fails with Cloudflare Tunnel despite allowedHosts + trustForwardingHeaders"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47682
---

# Voice-call plugin: Twilio webhook signature verification fails with Cloudflare Tunnel despite allowedHosts + trustForwardingHeaders

## 증상
The voice-call plugin's Twilio webhook signature verification fails when using a Cloudflare Tunnel as the public endpoint, even with `webhookSecurity.allowedHosts` and `trustForwardingHeaders: true` configured correctly.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
`skipSignatureVerification: true` — functional but disables an important security layer.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47682
