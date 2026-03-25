---
layout: solution
title: "Openclaw chrome extension not working Bug: Attach Current Tab fails on Chromium 145 Extension v1.0.2, Chrome/Edge/Chrome Beta all on Chromium 145 Error: code=1008 reason=invalid request frame"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/openclaw/comments/1rh3m3v/openclaw_chrome
---

# Openclaw chrome extension not working Bug: Attach Current Tab fails on Chromium 145 Extension v1.0.2, Chrome/Edge/Chrome Beta all on Chromium 145 Error: code=1008 reason=invalid request frame

## 증상
**Bug: Attach Current Tab fails on Chromium 145** Extension v1.0.2, Chrome/Edge/Chrome Beta all on Chromium 145 Error: `code=1008 reason=invalid request frame` Gateway connects fine but Attach Current Tab fails with invalid handshake. All WS permissions enabled. Password auth configured correctly.

  
I try everthing the gateway is connect and relay port but always when i do "attach current tab"



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
Reddit r/ClaudeAI https://reddit.com/r/openclaw/comments/1rh3m3v/openclaw_chrome_extension_not_working_bug_attach/
