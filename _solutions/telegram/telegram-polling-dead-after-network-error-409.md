---
layout: solution
title: "Telegram Polling Dead After Network Error — 409 Conflict Compound Failure"
category: telegram
description: "- 텔레그램 채널 상태 ON/OK인데 메시지 수발신이 안 됨 - 게이트웨이 로그에 sendMessage 시도 자체가 0건 - ETIMEDOUT, EHOSTUNREACH 에러 발생 후 상태가 고착됨 - 5번 해도 복구 안 됨 - curl로 봇 토큰 직접 API 호출했더니 409"
---

# Telegram 응답 안 감: 네트워크 에러 후 polling 죽음 + 409 conflict 복합 장애

## 증상
- 텔레그램 채널 상태 ON/OK인데 메시지 수발신이 안 됨
- 게이트웨이 로그에 sendMessage 시도 자체가 0건
- ETIMEDOUT, EHOSTUNREACH 에러 발생 후 상태가 고착됨
- `gateway restart` 5번 해도 복구 안 됨
- curl로 봇 토큰 직접 API 호출했더니 409 conflict 발생
- 웹챗은 정상, 텔레그램만 죽음

## 원인

**3가지 문제가 동시에 발생하는 복합 장애:**

### 원인 1: IPv6 실패 → Node.js 소켓 풀 고착 (GitHub #53338, #52116)
```
sendChatAction failed: Network request for 'sendChatAction' failed!
fetch fallback: enabling sticky IPv4-only dispatcher (codes=ETIMEDOUT,EHOSTUNREACH)
```
- IPv6로 Telegram API 연결 시도 → 실패 → IPv4 fallback 발동
- 하지만 내부 HTTP 클라이언트의 **소켓 풀이 깨진 상태로 고착**
- 게이트웨이 재시작해도 새 프로세스가 같은 패턴으로 즉시 재진입
- **결과: polling 시작하지만 실제로 getUpdates 호출을 안 함**

### 원인 2: curl 직접 호출 → 409 자기강화 루프 (GitHub #50064)
```
Telegram getUpdates conflict: 409: Conflict: terminated by other getUpdates request
```
- curl로 `getUpdates` 호출하면 게이트웨이 polling과 충돌
- 409 발생 후 **자기강화 루프 진입**: retry interval(30s) = long-poll timeout(30s)
- 이전 연결이 끊기기 전에 다음 retry가 시작 → 영구 409 루프

### 원인 3: probe/polling 이중 클라이언트 (GitHub #50064)
- `probeTelegram()` (health check)이 별도 TCP 연결 생성
- polling 클라이언트와 probe 클라이언트가 서로 409 유발
- health-monitor 5분 주기로 반복 → 409가 사라져도 다시 발생

## 해결법

### 즉시 해결 (마스터용, 지금 실행)

**Step 1: 모든 getUpdates 세션 강제 종료**
```bash
# 1. 게이트웨이 완전 정지
openclaw gateway stop

# 2. 남은 프로세스 확인 & 강제 종료
ps aux | grep openclaw | grep -v grep
kill -9 $(pgrep -f "openclaw")

# 3. Telegram 측 getUpdates 연결 초기화 (중요!)
# deleteWebhook + drop_pending_updates로 서버 측 연결 정리
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook?drop_pending_updates=true"

# 4. 10초 대기 (서버 측 long-poll 연결 타임아웃)
sleep 10

# 5. getUpdates 클린 상태 확인 (빈 배열이면 정상)
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates?timeout=1" | python3 -m json.tool
```

**Step 2: IPv4 강제 + 소켓 풀 리셋**
```bash
# openclaw.json 수정
# ~/.openclaw/openclaw.json 에서 telegram 섹션:
{
  "channels": {
    "telegram": {
      "enabled": true,
      "botToken": "<TOKEN>",
      "network": {
        "autoSelectFamily": false
      }
    }
  }
}
```

**Step 3: fetch.timeout 줄이기 (409 자기강화 루프 방지)**
```bash
# 이것은 런타임 패치가 필요함. 임시 환경변수로:
export OPENCLAW_TELEGRAM_POLL_TIMEOUT=10
```

**Step 4: 게이트웨이 클린 스타트**
```bash
# 게이트웨이 재시작
openclaw gateway start

# 로그 확인 — "starting provider" 이후 getUpdates 활동이 있어야 함
openclaw logs --tail 30 --filter telegram
```

**Step 5: 테스트**
```bash
# 텔레그램으로 테스트 메시지 전송
# 로그에 inbound message 표시되는지 확인
# 30초 내 응답이 와야 정상
```

### v2026.3.24로 업데이트 (권장)
```bash
# 2026.3.23-2에서 이 버그가 보고됨 → 2026.3.24에서 수정 가능성 확인
openclaw update
# 업데이트 후 Step 1-5 반복
```

### 재발 방지

1. **절대 curl로 getUpdates 직접 호출하지 않기** — 게이트웨이와 충돌
2. 네트워크 문제 발생 시 단순 restart 대신 **Step 1 전체 실행** (프로세스 kill + deleteWebhook + 대기)
3. 409 에러 보이면 **즉시 모든 openclaw 프로세스 kill 후 10초 대기** 후 재시작

## 예상 토큰 절약
이 에러로 삽질 시: 약 50,000~100,000 토큰 소비 (gateway restart 반복, 설정 변경 시행착오)
이 해결법 참조 시: 약 1,000 토큰

## 참고
- GitHub #50064: Telegram polling: self-sustaining 409 conflict from probe + health-monitor
- GitHub #53338: v2026.3.23 regression: Telegram polling dies after network errors
- GitHub #52116: Telegram polling client gets permanently stuck after transient network failure
- GitHub #4942: Telegram bot connects successfully but receives no messages
- GitHub #24546: Telegram long polling not starting (inbound messages not received)
- GitHub #24304: Telegram long polling not receiving messages on VPS
