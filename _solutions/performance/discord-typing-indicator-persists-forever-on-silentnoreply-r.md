---
layout: solution
title: "Discord typing indicator persists forever on silent/NO_REPLY runs"
category: performance
source: https://github.com/openclaw/openclaw/issues/27011
---

# Discord typing indicator persists forever on silent/NO_REPLY runs

## 증상
When an agent run ends with a silent reply (NO_REPLY / `SILENT_REPLY_TOKEN`), the Discord typing indicator ("X is typing...") persists indefinitely. The `triggerTyping` keepalive loop never stops.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Setting `agents.defaults.typingMode: "never"` disables the typing indicator entirely, preventing the bug but losing visual feedback.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27011
