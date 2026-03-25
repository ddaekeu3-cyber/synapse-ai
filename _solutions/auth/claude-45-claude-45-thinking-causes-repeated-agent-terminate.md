---
layout: solution
title: "Claude 4.5 / Claude 4.5 Thinking causes repeated “Agent terminated due to error” in Planning &amp; Fast modes"
category: auth
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1p98641/claud
---

# Claude 4.5 / Claude 4.5 Thinking causes repeated “Agent terminated due to error” in Planning &amp; Fast modes

## 증상
https://preview.redd.it/sl6oo0g6r24g1.jpg?width=616&amp;format=pjpg&amp;auto=webp&amp;s=f7165209123badbabff718024a77216904c1d403



# Title

**\[BUG\] Claude 4.5 workflows consistently crash in Antigravity (Planning/Fast) — “Agent terminated due to error”, Gemini 3 unaffected**

# Description

When using **Claude 4.5 Sonnet** or **Claude 4.5 Sonnet (thinking)** in Antigravity IDE, workflows in bot

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
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1p98641/claude_45_claude_45_thinking_causes_repeated/
