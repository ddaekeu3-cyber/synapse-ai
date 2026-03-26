---
layout: solution
title: "OpenClaw 텔레그램 429 + SIGTERM 재시작 루프 (반복 발생 패턴)"
category: telegram
---

# OpenClaw 텔레그램 429 + SIGTERM 재시작 루프

## 증상
- 텔레그램 sendMessage가 429 Too Many Requests로 실패
- sendChatAction이 "Network request failed"로 반복 실패
- 게이트웨이가 SIGTERM → 자동 재시작 → 또 실패 → 무한 루프
- `fetch fallback: enabling sticky IPv4-only dispatcher` 메시지 반복
- pending delivery가 deferred (backoff) 상태로 쌓임
- **단순 `gateway restart`로 복구 안 됨** — 재시작해도 같은 상태 재진입

## 원인
게이트웨이 재시작 시 이전 세션의 **pending delivery**가 즉시 재시도됨. 429 rate limit이 아직 풀리지 않은 상태에서 재시도 → 또 429 → SIGTERM → 재시작의 루프. IPv6 소켓 풀 고착이 동시에 발생하면 sendChatAction도 네트워크 실패.

핵심: **게이트웨이 재시작 = rate limit 리셋이 아님.** 서버 측 rate limit이 풀릴 때까지 기다려야 함.

## 해결법

### 5단계 클린 리셋 (이것만 따라하면 됨)

```bash
# 1. 완전 정지 + 프로세스 kill
openclaw gateway stop
sleep 1
kill -9 $(pgrep -f openclaw 2>/dev/null) 2>/dev/null

# 2. Telegram 서버 측 초기화
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook?drop_pending_updates=true"

# 3. 30초 대기 (rate limit 해소 — 이 단계 생략하면 안 됨!)
sleep 30

# 4. 클린 상태 확인
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates?timeout=1&limit=1"
# → ok:true, result:[] 이면 정상

# 5. 게이트웨이 시작
openclaw gateway start
```

### 왜 단순 restart가 안 되는가

| 동작 | 결과 |
|------|------|
| `gateway restart` | pending delivery 즉시 재시도 → 429 재발 |
| `gateway stop` → `gateway start` | 프로세스가 안 죽는 경우 있음 → 409 conflict |
| `kill -9` + 즉시 start | rate limit 안 풀림 → 429 재발 |
| **kill -9 + deleteWebhook + 30초 대기 + start** | **정상 복구** |

### IPv4 강제 설정 (재발 방지)

`~/.openclaw/openclaw.json`:
```json
{
  "channels": {
    "telegram": {
      "network": {
        "autoSelectFamily": false
      }
    }
  }
}
```

## 참고
- 이 문제는 반복 발생할 수 있음 (네트워크 불안정, Telegram API 지연 시)
- 핵심 원칙: **restart 전에 반드시 30초 이상 대기**
- 직접 경험 (2026-03-26). OpenClaw v2026.3.23-2. 같은 날 2회 발생.
- 관련: telegram-polling-dead-after-network-error-409.md
