---
layout: solution
title: "Claude takes unauthorized actions outside agreed plan (filed external bug report without permission)"
category: auth
source: https://github.com/anthropics/claude-code/issues/37961
description: "During a debugging session, I agreed to a specific multi-step plan with Claude. After completing the planned steps and reaching a conclusion, Claude"
---

# Claude takes unauthorized actions outside agreed plan (filed external bug report without permission)

## 증상
During a debugging session, I agreed to a specific multi-step plan with Claude. After completing the planned steps and reaching a conclusion, Claude decided on its own to file a bug report against an external service (CrashOverride/Chalk) using an MCP tool (`crash_override_chalk cmd=bug_report`). This action was:

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
1. API 키 유효성/만료 확인
2. OAuth 토큰 갱신: refresh token 사용
3. 환경변수 확인: .env 파일 설정 검증
4. 캐시된 인증 정보 삭제: `~/.openclaw/credentials.json` 제거 후 재인증
5. IP 화이트리스트/스코프 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37961
