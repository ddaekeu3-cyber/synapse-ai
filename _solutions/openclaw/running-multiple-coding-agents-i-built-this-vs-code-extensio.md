---
layout: solution
title: "Running multiple coding agents, I built this VS Code extension to better manage multiple Claude Code sessions by grouping them by task, and it's called AgentDock"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rym3zk/running_multi
---

# Running multiple coding agents, I built this VS Code extension to better manage multiple Claude Code sessions by grouping them by task, and it's called AgentDock

## 증상
Hey all,  
I noticed a lot of devs running multiple Claude Code agents at the same time, jumping between terminals trying to figure out which one was still thinking, which one crashed, and which one was just sitting idle eating context. It was kind of chaotic. I was doing the same thing myself and got tired of it, so I just built something to fix it.

So I built **AgentDock**, a VS Code extension 

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rym3zk/running_multiple_coding_agents_i_built_this_vs/
