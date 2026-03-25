---
layout: solution
title: "Claude Code 2.0.41"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1ozwogj/claude_code_2
---

# Claude Code 2.0.41

## 증상
Last week we shipped Claude Code 2.0.41 with enhanced UX improvements for the CLI including better loading indicators and inline permission handling, plus new plugin capabilities for output styles. We also delivered significant reliability improvements for Claude Code Web and Mobile, fixed several bugs around plugin execution and VS Code extension functionality.

**Features:**

**CLI**

* Improved

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1ozwogj/claude_code_2041/
