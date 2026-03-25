---
layout: solution
title: "WhatsApp proactive sends broken: requireActiveWebListener uses duplicated listeners Map"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51398
---

# WhatsApp proactive sends broken: requireActiveWebListener uses duplicated listeners Map

## 증상
Proactive WhatsApp sends via `message(action=send)`, `openclaw message send` CLI, hooks/agent, and cron delivery all fail with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Killing the gateway, sending via direct Baileys connection, letting gateway auto-restart. Causes ~15s blip.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51398
