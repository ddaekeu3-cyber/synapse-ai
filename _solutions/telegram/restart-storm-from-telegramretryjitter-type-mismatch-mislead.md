---
layout: solution
title: "restart storm from telegram.retry.jitter type mismatch + misleading doctor SecretRef for Telegram token"
category: telegram
---

# restart storm from telegram.retry.jitter type mismatch + misleading doctor SecretRef for Telegram token

## 증상
After host reboot, OpenClaw appeared "hung". Investigation showed a restart storm in a secondary runtime, plus confusing SecretRef diagnostics.

에러 메시지:
` was in an aggressive restart loop due to profile lock permission errors.

## Impact
- Severe perceived instability / "hang" state.
- Required manual host reboot by operator.
- Diagnostics are mislea

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52130 참조.

## 해결법
SecretRef "file:filemain:/providers/channels/telegram/botToken"`
- At the same time, runtime Telegram channel is actually healthy in `openclaw status` (`Telegram: ON/OK`) and provider starts successfully.
- A user-level gateway entered a fast restart loop due to invalid config type:
  - `channels.telegram.retry.jitter: Invalid input: expected number, received boolean`
- In parallel, `openclaw-chro

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52130
