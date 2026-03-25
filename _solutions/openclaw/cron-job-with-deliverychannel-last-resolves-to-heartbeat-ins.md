---
layout: solution
title: "Cron job with delivery.channel 'last' resolves to @heartbeat instead of actual Telegram chat"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45806
---

# Cron job with delivery.channel "last" resolves to @heartbeat instead of actual Telegram chat

## 증상
A one-shot cron job created via the cron tool with `delivery.channel: "last"` fails at delivery time because the "last route" resolves to `@heartbeat` (an internal session target) instead of the actual Telegram chat the user was communicating from when the job was created.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use explicit `delivery.channel` and `delivery.to` with numeric chat IDs instead of relying on `"last"`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45806
