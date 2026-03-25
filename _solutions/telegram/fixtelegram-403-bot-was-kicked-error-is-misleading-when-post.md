---
layout: solution
title: "fix(telegram): 403 'bot was kicked' error is misleading when posting to a locked forum topic"
category: telegram
source: https://github.com/openclaw/openclaw/issues/29048
---

# fix(telegram): 403 'bot was kicked' error is misleading when posting to a locked forum topic

## 증상
When a cron job is configured to deliver to a Telegram Forum topic that restricts posting (e.g. `topic:1` / General), the bot receives a `403: Forbidden: bot was kicked from the supergroup chat` error. This message is identical to the error when the bot is actually removed from the group, making root cause analysis extremely difficult.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Change `delivery.to` from `topic:1` (General) to a non-restricted topic ID. General topic (`topic:1`) in Telegram Forum groups defaults to admin-only posting.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29048
