---
layout: solution
title: "gateway connect failed"
category: openclaw
---

# gateway connect failed

## 증상
OpenClaw 2026.2.19-2 (45d9b20)  Will not connect to gateway in wsl.  Changed Windows so it coud see the the wsl loopback. Configured it to connect  Dangerously****: true. Still won't connect.

에러 메시지:
```shell
gateway connect failed: Error: pairing required
◇
[openclaw] Failed to start CLI: Error: gateway closed (1008): pairing required
Gateway target: ws://127.0.0.1:18790
Source: local loopback
Co

## 원인
원본 이슈에서 확인 필요. GitHub Issue #22445 참조.

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
https://github.com/openclaw/openclaw/issues/22445
