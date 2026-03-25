---
layout: solution
title: "OpenClaw Stuck in 'System Admin' Loop - Config Ignored, Agent:Main Won't Die (Mac Studio)"
category: config
source: Reddit r/ClaudeAI https://reddit.com/r/OpenclawBot/comments/1r0opl7/openclaw_stu
---

# OpenClaw Stuck in "System Admin" Loop - Config Ignored, Agent:Main Won't Die (Mac Studio)

## 증상
I’m hitting a major roadblock setting up OpenClaw on a Mac Studio and could really use some expert eyes on this.

The Goal:

I am trying to run a private, specialist agent called "Agent\_X" using Google Gemini 1.5 Pro. I need it for high-level research and planning, but it currently refuses to switch away from the "dumb" local model defaults.

The Setup:

• Environment: macOS (Mac Studio).

• Cons

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
Reddit r/ClaudeAI https://reddit.com/r/OpenclawBot/comments/1r0opl7/openclaw_stuck_in_system_admin_loop_config/
