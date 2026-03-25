---
layout: solution
title: "WhatsApp personal number connection (Baileys) announces online presence on connect, suppressing all phone push notifications"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/30286
---

# WhatsApp personal number connection (Baileys) announces online presence on connect, suppressing all phone push notifications

## 증상
When OpenClaw connects to a **personal WhatsApp number** via the Baileys/WhatsApp Web protocol, it announces presence as `available/online` by default. This causes WhatsApp to suppress all push notifications on the phone for every chat — not just the ones the agent is monitoring. This does **not** affect WhatsApp Business API connections.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
(checking WhatsApp manually) defeats the purpose of having an AI assistant monitor the account

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30286
