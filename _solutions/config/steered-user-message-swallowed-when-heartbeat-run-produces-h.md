---
layout: solution
title: "Steered user message swallowed when heartbeat run produces HEARTBEAT_OK"
category: config
source: https://github.com/openclaw/openclaw/issues/30197
---

# Steered user message swallowed when heartbeat run produces HEARTBEAT_OK

## 증상
When queue mode is set to `steer`, a user message that arrives during an active heartbeat run gets injected into that run. If the agent produces `HEARTBEAT_OK` (indicating no action needed) followed by a real response to the user's message, the outbound delivery appears to suppress the user-facing response along with the heartbeat ack.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Switching to `collect` mode avoids the issue, since the user message queues as a separate followup turn rather than being injected into the heartbeat run.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30197
