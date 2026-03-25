---
layout: solution
title: "Feature: Auto-leave WhatsApp groups not in allowlist + group-join webhook"
category: auth
source: https://github.com/openclaw/openclaw/issues/8495
---

# Feature: Auto-leave WhatsApp groups not in allowlist + group-join webhook

## 증상
When using WhatsApp with `groupPolicy: allowlist` and `groupAllowFrom`, unauthorized groups are blocked from triggering agent sessions — but the bot remains a member of those groups. This creates a security/privacy concern: the bot is still technically present and visible to group members.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
`groupAllowFrom` blocks messages but doesn't remove the bot from unauthorized groups. Manual cleanup required via WhatsApp app.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/8495
