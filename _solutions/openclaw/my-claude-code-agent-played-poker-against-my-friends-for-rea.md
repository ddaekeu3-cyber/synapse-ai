---
layout: solution
title: "My Claude Code agent played poker against my friends for real money (and it finished 2nd and knocked me out)"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1reijo9/my_claude_code_
---

# My Claude Code agent played poker against my friends for real money (and it finished 2nd and knocked me out)

## 증상
I've been building a rudimentary multi-agent system with Claude Code. One of my agents handles "investor relations" for me (I'm a publicly traded person... long story but shareholders vote on my life decisions).

Last month we let it play in our monthly poker tournament. Real money! I fronted it  the$50 buy-in.

**What happened:**

* It finished second out of the field. $50 → $165 (230% ROI).
* Mi

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1reijo9/my_claude_code_agent_played_poker_against_my/
