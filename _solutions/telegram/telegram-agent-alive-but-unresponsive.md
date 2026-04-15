---
layout: solution
title: "Telegram Agent Alive But Unresponsive — Silent Hang with No Error Logs"
category: telegram
description: "Everything shows healthy: Gateway running, Telegram ON/OK, session active — but agent does not respond to messages. No errors, no 429, no ETIMEDOUT. Silent hang." - 에러 로그 없음, 429 없음, ETIMEDOUT 없음 - 겉으로 보기엔 멀쩡한데 실제로 안 됨 - pending"
---

# OpenClaw 텔레그램 에이전트가 살아있는데 응답 안 하는 문제

## 증상
- `openclaw status` 전부 정상: Gateway running, Telegram ON/OK, 세션 active
- 하지만 텔레그램에서 메시지 보내도 에이전트가 응답하지 않음
- 에러 로그 없음, 429 없음, ETIMEDOUT 없음
- 겉으로 보기엔 멀쩡한데 실제로 안 됨
- pending delivery가 쌓여있을 수 있음

## 원인
게이트웨이 프로세스는 살아있지만 **내부 polling/message 처리 파이프라인이 고착**된 상태. 가능한 원인:

1. **polling 세션 고착**: getUpdates long-poll이 응답을 받았지만 내부 메시지 큐에 전달 안 됨
2. **세션 락**: 이전 요청이 완료되지 않아 새 메시지 처리가 대기 중 (maxConcurrent 제한)
3. **delivery recovery 루프**: 실패한 delivery가 반복 재시도되면서 새 메시지 처리 차단
4. **Node.js 이벤트 루프 블록**: 대용량 작업이 이벤트 루프를 점유하여 polling 콜백 지연

핵심: **에러가 없다고 정상이 아님.** 멈춤 자체가 이상 신호.

## 해결법

### 즉시 해결: 클린 리셋

```bash
# 1. 완전 정지
openclaw gateway stop
sleep 1
kill -9 $(pgrep -f openclaw 2>/dev/null) 2>/dev/null

# 2. Telegram 서버 측 초기화
curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true"

# 3. 10초 대기
sleep 10

# 4. 클린 스타트
openclaw gateway start
```

### 진단 방법

상태가 정상인데 응답이 없으면:

```bash
# 1. 로그에서 최근 에러 확인
grep -i "error\|fail\|429\|timeout" /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | tail -10

# 2. pending delivery 확인
grep "delivery" /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | tail -5

# 3. 세션 상태 확인
openclaw status 2>&1 | grep -A2 "Session"

# 4. 텔레그램 API 직접 테스트
curl -s "https://api.telegram.org/bot<TOKEN>/getMe"
```

전부 정상이면 → **클린 리셋이 유일한 해결법** (내부 상태 고착은 외부에서 진단 불가)

### 자동 감지 방법

하트비트에 텔레그램 응답 확인 추가:
```
하트비트 체크 시:
1. status가 OK인지 확인 (기존)
2. 마지막 텔레그램 송수신 시각 확인 (추가)
3. 30분 이상 송수신 없으면 → 이상 보고 (에러 없어도)
```

## 참고
- 직접 경험 (2026-03-27). OpenClaw v2026.3.23-2.
- 에러 없음 ≠ 정상. 진행이 없는 것 자체가 이상 신호.
- 관련: heartbeat-monitor-false-ok.md, telegram-429-sigterm-restart-loop.md
