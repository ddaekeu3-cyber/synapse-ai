---
layout: solution
title: "gateway/ws RPC send path broken after 3.13 upgrade — 'No active WhatsApp Web listener' despite healthy gateway"
category: openclaw
---

# gateway/ws RPC send path broken after 3.13 upgrade — "No active WhatsApp Web listener" despite healthy gateway

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
Gateway health shows WhatsApp: linked but send fails:

[gateway/ws] ⇄ res ✗ send 43ms errorCode=UNAVAILABLE 
errorMessage=Error: No active WhatsApp Web listener (account: default)

web-auto-r

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52574 참조.

## 해결법
available — downgrade not viable due to config incompatibility.
Inbound and auto-reply paths unaffected. Only the gateway/ws RPC outbound send path is broken.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52574
