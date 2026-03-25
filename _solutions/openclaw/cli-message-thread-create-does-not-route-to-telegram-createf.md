---
layout: solution
title: "CLI `message thread create` does not route to Telegram `createForumTopic`"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54273
---

# CLI `message thread create` does not route to Telegram `createForumTopic`

## 증상
`openclaw message thread create --channel telegram` fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Calling the Telegram Bot API directly (`createForumTopic`) works fine. The runtime-level `topic-create` action is fully functional — it's just the CLI entry point that's missing the Telegram routing.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54273
