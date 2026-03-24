---
layout: solution
title: "Agent tool errors should be routed to agent first, not directly to chat"
category: telegram
---

# Agent tool errors should be routed to agent first, not directly to chat

## 증상
When a tool call fails (e.g. `edit` with non-unique `oldText`, or any other tool error), OpenClaw currently routes the error message directly into the user-facing chat. The agent never gets a chance to self-correct silently.

에러 메시지:
```
⚠️ 📝 Edit: in ~/.openclaw/workspace/skills/openclaw-team-overview/web/index.html (59 chars) failed
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49882 참조.

## 해결법
automatically by the agent in the very next step.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49882
