---
layout: solution
title: "Charlotte v0.5.0 — structural tree view gives agents a complete page map in ~1,700 chars. Plus iframe support, file output, and 17 bug fixes."
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/mcp/comments/1rp2exz/charlotte_v050_struc
---

# Charlotte v0.5.0 — structural tree view gives agents a complete page map in ~1,700 chars. Plus iframe support, file output, and 17 bug fixes.

## 증상
Charlotte is a browser MCP server built for token efficiency. Where Playwright MCP sends the full accessibility tree on every call, Charlotte lets agents control how much detail they get back. v0.5.0 adds a new observation mode that makes the cheapest option even cheaper.

# The new tree view

`observe({ view: "tree" })` renders the page as a structural hierarchy instead of flat JSON:

    Stack O

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
Reddit r/ClaudeAI https://reddit.com/r/mcp/comments/1rp2exz/charlotte_v050_structural_tree_view_gives_agents/
