---
layout: solution
title: "Google Chat: Space @mentions silently dropped when botUser not configured"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/25639
description: "Google Chat Space @mention messages are silently dropped when is not configured in the googlechat channel config. DMs work perfectly, making this failure"
---

# Google Chat: Space @mentions silently dropped when botUser not configured

## 증상
Google Chat Space @mention messages are **silently dropped** when `botUser` is not configured in the googlechat channel config. DMs work perfectly, making this failure mode extremely confusing to debug.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually configure `botUser`:
```json
{
  "googlechat": {
    "botUser": "users/<numeric-bot-id>"
  }
}
```

Finding the ID requires: set `requireMention: false` temporarily, @mention bot, check logs for `userMention.user.name`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25639
