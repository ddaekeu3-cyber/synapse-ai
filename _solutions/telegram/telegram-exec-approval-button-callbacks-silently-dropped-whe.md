---
layout: solution
title: "Telegram exec approval button callbacks silently dropped when execApprovals not set on channel config"
category: telegram
source: https://github.com/openclaw/openclaw/issues/45670
---

# Telegram exec approval button callbacks silently dropped when execApprovals not set on channel config

## 증상
Exec approval buttons render correctly in Telegram DM but pressing them has no effect. The approval times out after 120 seconds despite repeated button presses.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Add `execApprovals` to the Telegram channel config:

```json
{
  "channels": {
    "telegram": {
      "execApprovals": {
        "enabled": true,
        "approvers": ["<telegram_user_id>"]
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45670
