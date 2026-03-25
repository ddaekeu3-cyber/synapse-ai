---
layout: solution
title: "[BUG in Google Antigravity] Claude models fail with “Agent execution terminated” error when Firebase MCP Tool #15 is enabled"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1q6f7ak/bug_i
---

# [BUG in Google Antigravity] Claude models fail with “Agent execution terminated” error when Firebase MCP Tool #15 is enabled

## 증상
# * UPDATE: solved as of the latest Antigravity Version: 1.14.2 *



**Bug description:**

In Google Antigravity, there is a critical execution conflict between the Firebase MCP server and Anthropic Claude models (Sonnet/Opus). When Firebase MCP Tool #15 (functions\_get\_logs) is enabled, Claude models fail to execute any prompt, whereas Google Gemini models continue to function correctly.

**Note

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
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1q6f7ak/bug_in_google_antigravity_claude_models_fail_with/
