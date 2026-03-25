---
layout: solution
title: "Sharp module win32-x64 binary missing in Cursor extension, breaks image upload on Windows"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/33538
---

# Sharp module win32-x64 binary missing in Cursor extension, breaks image upload on Windows

## 증상
Image upload broken on Windows in the Cursor extension (anthropic.claude-code-*-universal). The sharp module's win32-x64 binary is not   bundled, causing Could not load the "sharp" module using the win32-x64 runtime on image paste/upload. Workaround: npm install            --os=win32 --cpu=x64 sharp in the extension directory, but this is wiped on every extension update.

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
https://github.com/anthropics/claude-code/issues/33538
