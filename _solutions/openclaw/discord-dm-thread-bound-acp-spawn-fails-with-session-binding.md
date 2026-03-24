---
layout: solution
title: "Discord DM thread-bound ACP spawn fails with 'Session binding adapter failed to bind target conversation'"
category: openclaw
---

# Discord DM thread-bound ACP spawn fails with 'Session binding adapter failed to bind target conversation'

## 증상
Discord DM `thread:true` ACP spawns fail with `Session binding adapter failed to bind target conversation`, while plain ACP runs succeed.

에러 메시지:
`Session binding adapter failed to bind target conversation`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #41116 참조.

## 해결법
`channelId`
- in a DM context this appears to fall through to null, producing the generic bind failure

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/41116
