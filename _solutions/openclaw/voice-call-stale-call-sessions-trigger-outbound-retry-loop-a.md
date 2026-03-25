---
layout: solution
title: "[voice-call] Stale call sessions trigger outbound retry loop after gateway restart — calls user repeatedly at night"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48739
---

# [voice-call] Stale call sessions trigger outbound retry loop after gateway restart — calls user repeatedly at night

## 증상
After a gateway restart, the voice-call plugin restores stale call sessions and marks them as `older than maxDurationSeconds` (correctly skipped) — but **still enters a 5-minute outbound retry loop**, repeatedly calling the user via Twilio REST API until the gateway is manually restarted again.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Restarting the gateway again (SIGUSR1 or `launchctl stop/start`) clears the plugin state and stops the retry loop. Alternatively, cancelling active Twilio calls via REST API:

```bash
curl -X POST -u "$ACCOUNT_SID:$AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$ACCOUNT_SID/Calls/$CALL_SID.json" \
  -d "Status=completed"
```

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48739
