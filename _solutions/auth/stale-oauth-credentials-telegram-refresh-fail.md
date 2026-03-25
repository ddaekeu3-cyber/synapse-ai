---
layout: solution
title: "OAuth sync imports stale credentials, Telegram fails with refresh_token error"
category: auth
source: https://github.com/openclaw/openclaw/issues/54050
---

# OAuth sync imports stale credentials, Telegram fails with refresh_token error

## 증상
OpenAI Codex OAuth sync appears to import stale credentials. Telegram channel then fails with refresh_token_not_found error. Multiple channels break simultaneously.

## 원인
OAuth credential sync pulls expired tokens from cache. Gateway uses these stale tokens and fails to refresh, cascading failures to dependent channels.

## 해결법
### OAuth 인증 정보 동기화 실패 해결
1. 캐시된 토큰 삭제: `~/.openclaw/oauth-tokens/` 디렉토리 비우기
2. Gateway 재시작 전 `OPENCLAW_OAUTH_CACHE=false` 환경변수 설정
3. Telegram 토큰 재발급: BotFather에서 새 토큰 생성
4. 수동 재인증: `openclaw auth refresh --force`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54050
