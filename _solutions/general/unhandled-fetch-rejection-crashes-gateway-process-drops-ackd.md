---
layout: solution
title: "Unhandled fetch rejection crashes gateway process, drops ack'd messages"
category: general
---

# Unhandled fetch rejection crashes gateway process, drops ack'd messages

## 증상
An unhandled `TypeError: fetch failed` from Node's undici (native fetch) crashes the entire gateway process. Messages that have been received and ack-reacted but not yet processed by the LLM are lost on restart.

에러 메시지:
```
[clawdbot] Unhandled promise rejection: TypeError: fetch failed
    at node:internal/deps/undici/undici:14902:13
    at processTicksAndRejections (node:internal/process/task_queues:105:5)
    at r

## 원인
원본 이슈에서 확인 필요. GitHub Issue #50236 참조.

## 해결법
None currently. The gateway restart loop means some messages are silently dropped.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50236
