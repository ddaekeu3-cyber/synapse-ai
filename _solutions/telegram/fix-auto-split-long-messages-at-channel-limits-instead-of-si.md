---
layout: solution
title: "fix: auto-split long messages at channel limits instead of silent failure"
category: telegram
source: https://github.com/openclaw/openclaw/issues/47909
---

# fix: auto-split long messages at channel limits instead of silent failure

## 증상
When an agent sends a message exceeding the channel's character limit, the message silently fails. The user sees nothing — no error, no partial message, no notification.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Agents must manually check message length and split — but this is unreliable across models and easy to forget, especially in heartbeat/automated responses.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47909
