---
layout: solution
title: "Bug: Telegram message target with :topic:<id> sends to current topic instead of requested topic"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52743
---

# Bug: Telegram message target with :topic:<id> sends to current topic instead of requested topic

## 증상
`message` tool sends to the current Telegram topic instead of the requested forum topic when `target` encodes `:topic:<threadId>`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- `action=send`
- `channel=telegram`
- `target=<forum-supergroup-chat-id>`
- `threadId=<target-topic-id>`
- `message="test"`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52743
