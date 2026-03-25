---
layout: solution
title: "Self-improvement Loop: My favorite Claude Code Skill"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1r89084/selfimproveme
---

# Self-improvement Loop: My favorite Claude Code Skill

## 증상
I've built a bunch of custom skills for Claude Code. Some are clever. Some are over-engineered. The one I actually use every single session is basically a glorified checklist.

It's called `wrap-up`. I run it at the end of every working session. It commits code, checks if I learned anything worth remembering, reviews whether Claude made mistakes it should learn from, and flags anything worth publi

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1r89084/selfimprovement_loop_my_favorite_claude_code_skill/
