---
layout: solution
title: "Compaction status messages leak into Telegram even when /verbose off is set"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54052
---

# Compaction status messages leak into Telegram even when /verbose off is set

## 증상
In a Telegram DM session, OpenClaw surfaces `🧹 Compacting context...` messages into chat even after `/verbose off` is explicitly set. This appears to be separate from the recently fixed LiteLLM timeout issue.

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
https://github.com/openclaw/openclaw/issues/54052
