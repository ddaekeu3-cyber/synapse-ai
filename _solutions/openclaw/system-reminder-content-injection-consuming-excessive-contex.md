---
layout: solution
title: "'System reminder' content injection consuming excessive context tokens"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/4464
---

# "System reminder" content injection consuming excessive context tokens

## 증상
Claude Code sometimes automatically injects file contents into the model's context when certain files are modified, using "system reminder" notifications. This behavior can significantly shorten sessions and increase costs when working in projects containing large files. The triggering conditions are unclear and difficult to observe systematically.

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
https://github.com/anthropics/claude-code/issues/4464
