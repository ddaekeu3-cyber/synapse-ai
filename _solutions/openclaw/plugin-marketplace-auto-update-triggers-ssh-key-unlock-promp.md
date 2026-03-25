---
layout: solution
title: "Plugin marketplace auto-update triggers SSH key unlock prompt on every new session (Linux/gnome-keyring)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37886
---

# Plugin marketplace auto-update triggers SSH key unlock prompt on every new session (Linux/gnome-keyring)

## 증상
Since the plugin marketplace was introduced (~March 20, 2026), Claude Code triggers an SSH key unlock prompt (gnome-keyring) at the start of every new session in a new project directory. This is alarming to users — it looks like a potential security incident.

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
https://github.com/anthropics/claude-code/issues/37886
