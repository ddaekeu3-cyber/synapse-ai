---
layout: solution
title: "OpenClaw 텔레그램 세션 분열: 대화 길어지면 세션 2개로 갈라져 응답 꼬임"
category: telegram
description: "- 텔레그램에서 대화가 길어지면 세션이 2개로 분열됨 - 1번 세션(이전 세션)이 먼저 반응해서 응답이 꼬임 - 같은 채팅에서 두 세션이 동시에 경합하며 중복/충돌 응답 발생 - 사용자 입장에서 봇이 엉뚱한 컨텍스트로 답변하거나 두 번"
---

# OpenClaw 텔레그램 세션 분열: 대화 길어지면 세션 2개로 갈라져 응답 꼬임

## 증상
- 텔레그램에서 대화가 길어지면 세션이 2개로 분열됨
- 1번 세션(이전 세션)이 먼저 반응해서 응답이 꼬임
- 같은 채팅에서 두 세션이 동시에 경합하며 중복/충돌 응답 발생
- 사용자 입장에서 봇이 엉뚱한 컨텍스트로 답변하거나 두 번 응답함

## 원인
OpenClaw 게이트웨이가 컨텍스트 포화(context saturation) 시 새 세션을 자동 생성하는데, 이전 세션이 즉시 종료되지 않고 살아있어서 두 세션이 동시에 같은 채팅의 메시지를 수신. 이전 세션(짧은 컨텍스트)이 새 세션(세션 초기화 중)보다 빠르게 반응하여 응답 우선권을 가져감.

## 해결법

### 메시지 수신 레이턴시 설정 (0.5초 딜레이)

`~/.openclaw/openclaw.json`에서 텔레그램 채널에 응답 지연 설정:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "botToken": "<TOKEN>",
      "responseDelay": 500,
      "session": {
        "dmScope": "main",
        "maxConcurrent": 1
      }
    }
  }
}
```

### maxConcurrent 제한으로 세션 경합 방지

```json
{
  "agents": {
    "main": {
      "maxConcurrent": 1
    }
  }
}
```

`maxConcurrent: 1`로 설정하면 동일 에이전트에서 동시에 하나의 요청만 처리. 이전 세션이 살아있어도 새 메시지가 큐에 쌓여서 순서대로 처리됨.

### 세션 정리

이미 분열된 상태라면:

```bash
# 현재 활성 세션 확인
openclaw sessions list

# 오래된/중복 세션 종료
openclaw sessions close <old-session-id>

# 또는 전체 리셋
openclaw sessions close --all
openclaw gateway restart
```

### 근본 해결: 컨텍스트 포화 전 수동 리셋

대화가 길어지기 전에 주기적으로 세션 리셋:

```
/new   ← 새 세션 시작 (이전 세션 깨끗하게 종료)
```

## 참고
- 대화가 20턴 이상이거나 컨텍스트가 80% 이상 차면 분열 위험 증가
- `dmScope: "main"` 설정이 되어있어야 같은 DM에서 세션이 하나로 유지됨
- 직접 경험 (2026-03-26). OpenClaw v2026.3.23-2 환경.
