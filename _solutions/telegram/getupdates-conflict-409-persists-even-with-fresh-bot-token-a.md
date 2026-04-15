---
layout: solution
title: "getUpdates conflict (409) persists even with fresh bot token and health monitor disabled"
category: telegram
source: https://github.com/openclaw/openclaw/issues/49822
description: "Behavior bug (incorrect output/state without"
---

# getUpdates conflict (409) persists even with fresh bot token and health monitor disabled

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
from PR #31141 (merged March 2) does not resolve this issue - the polling loop leak persists.
---
Workarounds Attempted (None Successful)
1. ✅ Disabled health monitor (channelHealthCheckMinutes: 0)
2. ✅ Used fresh Telegram bot token
3. ✅ Complete container stop/start cycle (not restart)
4. ✅ Cleared webhooks via Telegram API
5. ✅ Built from stable release v2026.3.13-1
6. ✅ Verified single container/process
---
Additional Context
- Container starts healthy and reports no initial conflicts
- Conflict appears consistently 60-120 seconds after start
- Issue occurs with both minimal configuration a

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49822
