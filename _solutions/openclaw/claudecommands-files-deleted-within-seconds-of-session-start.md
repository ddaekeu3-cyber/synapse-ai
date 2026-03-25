---
layout: solution
title: ".claude/commands/ files deleted within seconds of session start (v2.1.81)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38326
---

# .claude/commands/ files deleted within seconds of session start (v2.1.81)

## 증상
Files in `.claude/commands/` are deleted within seconds of a Claude Code session starting. The files exist on disk before launch, are briefly visible in the session (slash commands appear momentarily), then vanish. After deletion, `/skills` shows no user commands.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38326
