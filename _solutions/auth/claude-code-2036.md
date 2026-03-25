---
layout: solution
title: "Claude Code 2.0.36"
category: auth
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1oraxj9/claude_code_203
---

# Claude Code 2.0.36

## 증상
This week we shipped Claude Code 2.0.36 with Claude Code on the Web enhancements, un-deprecated output styles based on community feedback, and improved command handling. We also extended free credits for Claude Code on the Web until November 18th and fixed several critical bugs around message queuing, MCP OAuth connections, and large file handling.

**Features:**

* Claude Code on the Web now incl

## 원인
보고된 버그/문제. 카테고리: auth.

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1oraxj9/claude_code_2036/
