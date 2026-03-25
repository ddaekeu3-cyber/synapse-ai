---
layout: solution
title: "Feature Request: Pass TELEGRAM_MESSAGE_THREAD_ID to Skills"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43402
---

# Feature Request: Pass TELEGRAM_MESSAGE_THREAD_ID to Skills

## 증상
Currently, when a Skill is invoked in a Telegram Topic (Thread), OpenClaw passes `TELEGRAM_CHAT_ID` to the Skill environment, but it does NOT pass `TELEGRAM_MESSAGE_THREAD_ID`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently users must call Skills from desktop Telegram (where Chat ID is consistent), but mobile users experience message routing issues.

---
*Filed from OpenClaw user community*

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43402
