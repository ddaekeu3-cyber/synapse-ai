---
layout: solution
title: "System prompt completely missing after /new — workspace files not injected, silent failure"
category: openclaw
---

# System prompt completely missing after /new — workspace files not injected, silent failure

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
**Timeline (UTC+8, 2026-03-22):**

| Time | Event |
|------|-------|
| 22:39-22:41 | Agent rewrites AGENTS.md, HEARTBEAT.md, TOOLS.md, SOUL.md via tool calls |
| 22:42-22:43 | Agent re-reads 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52317 참조.

## 해결법
it immediately.

### Steps to reproduce

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52317
