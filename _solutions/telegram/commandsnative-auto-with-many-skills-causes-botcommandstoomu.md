---
layout: solution
title: "commands.native: 'auto' with many skills causes BOT_COMMANDS_TOO_MUCH crash loop on Telegram"
category: telegram
source: https://github.com/openclaw/openclaw/issues/27456
---

# commands.native: "auto" with many skills causes BOT_COMMANDS_TOO_MUCH crash loop on Telegram

## 증상
When `commands.native` is set to `"auto"` and a large number of skills are installed (60+), the gateway attempts to register all skills as native Telegram bot commands on every startup. This exceeds Telegram's bot command limit and causes a `BOT_COMMANDS_TOO_MUCH` error on every boot, resulting in a crash loop.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Set in `openclaw.json`:
```json
{
  "commands": {
    "native": false,
    "nativeSkills": false
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27456
