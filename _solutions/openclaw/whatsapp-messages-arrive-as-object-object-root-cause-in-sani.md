---
layout: solution
title: "WhatsApp messages arrive as [object Object] — root cause in sanitizeChatSendMessageInput()"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52464
---

# WhatsApp messages arrive as [object Object] — root cause in sanitizeChatSendMessageInput()

## 증상
WhatsApp messages intermittently arrive to the LLM as `[object Object]` instead of the actual text. The bot sees garbled input and cannot process the user's request.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Added an instruction in AGENTS.md telling the bot to ask the user to repeat their message when it receives garbled input, rather than treating it as a heartbeat or attempting tool calls.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52464
