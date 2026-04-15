---
layout: solution
title: "Telegram plugin crashes with 409 error due to orphaned polling processes on session restart"
category: telegram
source: https://github.com/anthropics/claude-code/issues/38204
description: "The Telegram channel plugin (grammy-based long polling) crashes with a 409 Conflict error when a new Claude Code session starts, because the previous bot"
---

# Telegram plugin crashes with 409 error due to orphaned polling processes on session restart

## 증상
The Telegram channel plugin (grammy-based long polling) crashes with a 409 Conflict error when a new Claude Code session starts, because the previous bot instance's `getUpdates` polling loop is still running. This means the Telegram channel becomes unreachable after every session restart or `/clear`.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Manually kill orphaned Telegram processes before starting a new session:

```bash
pkill -f "telegram/0.0.1"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38204
