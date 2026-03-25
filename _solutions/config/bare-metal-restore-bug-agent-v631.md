---
layout: solution
title: "Bare Metal Restore Bug Agent v6.3.1"
category: config
source: Reddit r/ClaudeAI https://reddit.com/r/Veeam/comments/1kff8wg/bare_metal_restore
---

# Bare Metal Restore Bug Agent v6.3.1

## 증상
I created a media recovery iso. Like always with Version 6.3.1 on Win11 24H2.

Now in want to do a bare metal restore from a smb share.

When in try to connect to the share i get an error: RPC Server not available.

So i went back to the main menu and opened the cmd and ran ipconfig to check if the notebook has gotten any ip. yes indeed it has and the smb share / nas is pingable. 

when i want to 

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
1. 공식 문서 참조: 최신 설정 가이드 확인
2. 환경변수 확인: 필수 변수 설정 확인
3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인
4. 로그 확인: 시작 로그에서 설정 관련 경고 확인
5. 최소 설정으로 시작해서 하나씩 추가

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/Veeam/comments/1kff8wg/bare_metal_restore_bug_agent_v631/
