---
layout: solution
title: "Discord: forwarded messages are silently dropped on inbound"
category: config
source: https://github.com/openclaw/openclaw/issues/40713
---

# Discord: forwarded messages are silently dropped on inbound

## 증상
When someone forwards a Discord message from one channel to another, the forwarded content is silently stripped from the inbound message. The agent receives an empty message — no text, no context, nothing to work with.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
For now, replying to a forwarded message (instead of forwarding directly) does pass through the forwarded content, since reply context has `includeForwarded: true`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40713
