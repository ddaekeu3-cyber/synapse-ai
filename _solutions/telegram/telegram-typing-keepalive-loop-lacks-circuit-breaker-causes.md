---
layout: solution
title: "Telegram typing keepalive loop lacks circuit breaker, causes gateway crash on network failure"
category: telegram
source: https://github.com/openclaw/openclaw/issues/45759
---

# Telegram typing keepalive loop lacks circuit breaker, causes gateway crash on network failure

## 증상
When the Telegram API becomes unreachable (network blip, DNS timeout, etc.), the typing indicator keepalive loop (`createTypingKeepaliveLoop` in `src/channels/typing-lifecycle.ts`) continues firing `sendChatAction` calls every 6 seconds indefinitely. Each failed call triggers up to 3 retries with exponential backoff (up to 30s). Multiple concurrent typing contexts compound this, saturating the eve

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Setting `agents.defaults.typingMode: "never"` in `openclaw.json` eliminates the crash vector entirely. Additionally reducing `channels.telegram.retry.attempts` to `1` and `timeoutSeconds` to `5` limits blast radius.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45759
