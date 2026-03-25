---
layout: solution
title: "built a tool to easily share web app bugs with coding agents like Antigravity"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/VibeCodersNest/comments/1pase37/built_a_t
---

# built a tool to easily share web app bugs with coding agents like Antigravity

## 증상
https://reddit.com/link/1pase37/video/7i73wi2x514g1/player

I’ve been exploring how to share web app bugs with coding agents like Antigravity.

The antigravity browser focus on letting its agent reproduce the issue itself, but often I’ve already found the bug and just need a way to show antigravity the exact context.

So we built FlowLens, an [open-source MCP server](https://github.com/magentic/fl

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
Reddit r/ClaudeAI https://reddit.com/r/VibeCodersNest/comments/1pase37/built_a_tool_to_easily_share_web_app_bugs_with/
