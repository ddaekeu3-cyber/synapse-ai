---
layout: solution
title: "MCP OAuth token refresh not persisting for Notion MCP server"
category: auth
source: https://github.com/anthropics/claude-code/issues/28256
description: "The Notion MCP server (installed via the official marketplace plugin) requires re-authentication on nearly every session start, despite having a valid"
---

# MCP OAuth token refresh not persisting for Notion MCP server

## 증상
The Notion MCP server (installed via the official marketplace plugin) requires re-authentication on nearly every session start, despite having a valid refresh token stored in `~/.claude/.credentials.json`. The expected behavior is that Claude Code automatically uses the refresh token to obtain a new access token when the current one expires.

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
https://github.com/anthropics/claude-code/issues/28256
