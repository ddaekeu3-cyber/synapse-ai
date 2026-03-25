---
layout: solution
title: "LSP is coming to Claude Code and you can try it now"
category: config
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1otdfo9/lsp_is_coming_t
---

# LSP is coming to Claude Code and you can try it now

## 증상
**TL;DR**

As of 2.0.30, Claude Code supports LSP servers.  It's still raw though, so you need to use [tweakcc ](https://github.com/Piebald-AI/tweakcc)to patch your CC to make them work.  Just run `npx tweakcc --apply` and install example plugins with LSP servers via `/plugin marketplace add Piebald-AI/claude-code-lsps`.

**Deep Dive**

Claude Code 2.0.30 introduced the beginnings of a fully featu

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1otdfo9/lsp_is_coming_to_claude_code_and_you_can_try_it/
