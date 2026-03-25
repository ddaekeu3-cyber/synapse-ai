---
layout: solution
title: "Subagents fail with 'prompt is too long' when user has many MCP servers (tool definitions exceed 200k)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37793
---

# Subagents fail with 'prompt is too long' when user has many MCP servers (tool definitions exceed 200k)

## 증상
When a user has many MCP servers configured at the user level, subagents (Explore, Plan, general-purpose) fail immediately with `prompt is too long: 209117 tokens > 200000 maximum` before executing a single tool call. The TUI shows `Done (0 tool uses · 0 tokens · 44s)` with no error visible to the user.

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
https://github.com/anthropics/claude-code/issues/37793
