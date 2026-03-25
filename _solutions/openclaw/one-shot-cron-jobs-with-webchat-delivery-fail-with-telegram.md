---
layout: solution
title: "One-shot cron jobs with webchat delivery fail with Telegram validation error"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47978
---

# One-shot cron jobs with webchat delivery fail with Telegram validation error

## 증상
**OpenClaw Version:** 2026.3.13-1

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
For one-shot reminders:
- Use `channel: "telegram"` with valid `to: "<chatId>"` (works)
- Or use recurring cron jobs for webchat delivery (works)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47978
