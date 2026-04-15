---
layout: solution
title: "ClawHub login loop after account deletion — OAuth completes but no redirect"
category: auth
source: https://github.com/openclaw/openclaw/issues/54076
description: "After deleting and recreating account, ClawHub login enters infinite loop. OAuth flow completes successfully but redirect back to ClawHub never happens."
---

# ClawHub login loop after account deletion — OAuth completes but no redirect

## 증상
After deleting and recreating account, ClawHub login enters infinite loop. OAuth flow completes successfully but redirect back to ClawHub never happens. Stuck on loading screen.

## 원인
Stale OAuth tokens/sessions cached from the deleted account conflict with new account. Browser cookies and local storage retain old session data.

## 해결법
### ClawHub 로그인 루프 해결
1. 브라우저 쿠키 + 로컬 스토리지 전체 삭제 (ClawHub 도메인)
2. `~/.openclaw/credentials.json` 삭제
3. GitHub OAuth 앱에서 이전 권한 취소 (Settings → Applications)
4. 시크릿/프라이빗 브라우저 창에서 재로그인 시도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54076
