---
layout: solution
title: "We traced every API call Claude Code makes. Here's how subdirectory CLAUDE.md files actually work"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rwhkf7/we_traced_every
---

# We traced every API call Claude Code makes. Here's how subdirectory CLAUDE.md files actually work

## 증상
I had questions about how CLAUDE.md files actually work in Claude Code agents — so I built a proxy and traced every API call

## First: the different types of CLAUDE.md

Most people know you can put a `CLAUDE.md` at your project root and Claude will pick
it up. But Claude Code actually supports them at multiple levels:

- **Global** (`~/.claude/CLAUDE.md`) — your personal instructions across all p

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rwhkf7/we_traced_every_api_call_claude_code_makes_heres/
