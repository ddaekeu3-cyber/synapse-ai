---
layout: solution
title: "Control UI Pairing Flow Fails with Gateway Timeout"
category: openclaw
---

# Control UI Pairing Flow Fails with Gateway Timeout

## 증상
Regression (worked before, now fails)

에러 메시지:
```json
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "lan",
    "trustedProxies": ["127.0.0.1"],
    "controlUi": {
      "allowedOrigins": ["https://[PUBLIC_IP]:12036"]
    },

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45753 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45753
