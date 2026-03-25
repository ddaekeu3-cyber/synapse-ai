---
layout: solution
title: "Discord REST API ignores proxy configuration"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28788
---

# Discord REST API ignores proxy configuration

## 증상
When Discord channel is configured with a proxy, the WebSocket connection works correctly, but REST API calls (sending messages) fail with `TypeError: fetch failed`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using Discord Webhook for push notifications (works fine), but cannot receive/respond to channel messages.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28788
