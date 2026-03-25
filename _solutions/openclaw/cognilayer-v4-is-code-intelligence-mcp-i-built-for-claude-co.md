---
layout: solution
title: "CogniLayer v4 is code intelligence MCP I built for Claude Code. Tree-sitter AST, symbol search, blast radius, subagent compression,cross-project memory. Free, runs locally"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rjw6do/cognilayer_v4_i
---

# CogniLayer v4 is code intelligence MCP I built for Claude Code. Tree-sitter AST, symbol search, blast radius, subagent compression,cross-project memory. Free, runs locally

## 증상
 I've been working on CogniLayer for the past few days and just shipped a major update, so I wanted to share where it's at now.

What it is: An MCP server I built for Claude Code (and Codex CLI) that adds two things the agent doesn't have natively - code intelligence and structured knowledge across sessions.

Code intelligence (the new stuff):

Tree-sitter AST parsing across 10+ languages. Not gre

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rjw6do/cognilayer_v4_is_code_intelligence_mcp_i_built/
