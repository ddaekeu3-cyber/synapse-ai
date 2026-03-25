---
layout: solution
title: "Telegram plugin fails to load during heartbeat embedded runs"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/36425
---

# Telegram plugin fails to load during heartbeat embedded runs

## 증상
During heartbeat embedded runs, the Telegram plugin fails to load with a module not found error. This causes all heartbeat responses to silently fail delivery - they never reach Telegram.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using direct Telegram API calls bypasses OpenClaw's internal delivery:
```bash
curl -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="$CHAT_ID" \
  -d text="Message"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36425
