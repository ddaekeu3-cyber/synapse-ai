---
layout: solution
title: "Docker install => EACCES: permission denied, mkdir '/home/node'"
category: gog
---

# Docker install => EACCES: permission denied, mkdir '/home/node'

## 증상
Docker installation fails with error `Error: EACCES: permission denied, mkdir '/home/node/.clawdbot/agents/main/agent'`

에러 메시지:
```
Failed to move legacy state dir (/home/node/.clawdbot → /home/node/.moltbot): Error:
EBUSY: resource busy or locked, rename '/home/node/.clawdbot' -> '/home/node/.moltbot'
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #3480 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/3480
