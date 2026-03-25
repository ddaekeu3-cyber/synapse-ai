---
layout: solution
title: "Discord image uploads fail with server_error in channel sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42238
---

# Discord image uploads fail with server_error in channel sessions

## 증상
$## Summary\nUploading images in a Discord-backed OpenClaw channel fails instead of reaching the agent.\n\n## Context\nChannel: Discord\nSurface: channel session\nObserved while trying to send an image to the agent in `#clu-assistant-gpt`.\n\n## User-visible error\n```json\n{"type":"error","error":{"type":"server_error","code":"server_error","message":"An error occurred while processing your reque

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
https://github.com/openclaw/openclaw/issues/42238
