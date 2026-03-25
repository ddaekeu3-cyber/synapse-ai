---
layout: solution
title: "600-second timeout in webview silently drops saved session ID on panel restore"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/35005
---

# 600-second timeout in webview silently drops saved session ID on panel restore

## 증상
The VS Code extension webview applies a 10-minute (600-second) expiry to the session ID saved in panel state. If the panel's last activity was more than 10 minutes before VS Code is closed (or before the next restart), the saved session ID is silently discarded and the panel opens a blank new session.

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
https://github.com/anthropics/claude-code/issues/35005
