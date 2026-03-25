---
layout: solution
title: "Feature request: HTTP webhook to trigger agent tasks directly"
category: telegram
source: https://github.com/openclaw/openclaw/issues/10448
---

# Feature request: HTTP webhook to trigger agent tasks directly

## 증상
Allow external services to trigger agent tasks via HTTP webhook, without needing to relay through a messaging channel (Telegram/Discord/etc).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Sending task prompts through Telegram as a relay, which creates noise in the chat and requires the messaging channel to be active.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/10448
