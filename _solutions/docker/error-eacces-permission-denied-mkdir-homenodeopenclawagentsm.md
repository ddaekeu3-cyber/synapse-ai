---
layout: solution
title: "Error: EACCES: permission denied, mkdir '/home/node/.openclaw/agents/main/agent'"
category: docker
---

# Error: EACCES: permission denied, mkdir '/home/node/.openclaw/agents/main/agent'

## 증상
I'm trying to follow [Quick start](https://docs.openclaw.ai/install/docker#quick-start-recommended) and while running `./docker-setup.sh`, I end up with this error:

에러 메시지:
```
# echo $?
1
#
```

### OpenClaw version

2026.2.20

### Operating system

Linux rpi5 6.12.34+rpt-rpi-2712 #1 SMP PREEMPT Kali 1:6.12.34-1+rpt1+0kali2 (2025-07-21) aarch64 GNU/Linux

### Install me

## 원인
원본 이슈에서 확인 필요. GitHub Issue #21571 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/21571
