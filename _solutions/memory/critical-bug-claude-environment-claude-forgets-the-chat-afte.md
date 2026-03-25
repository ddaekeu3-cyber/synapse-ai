---
layout: solution
title: "Critical Bug - Claude Environment - Claude forgets the chat after Error during execution error"
category: memory
source: Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1r1xboo/critical_b
---

# Critical Bug - Claude Environment - Claude forgets the chat after Error during execution error

## 증상
https://preview.redd.it/vt1ga2e3avig1.png?width=1435&amp;format=png&amp;auto=webp&amp;s=dc0f4277d655ddc40ab0a3cd6658fbbd9498a3d7

So imagine, the AI almost finished your code, it crashes, and forgets everything, even when the chat is there.

Marvelous.

I showed this to AI and he said that it's context loss

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
1. 영속적 메모리 파일: CLAUDE.md에 핵심 정보 기록
2. 세션 요약 자동 저장: 종료 시 진행상황 파일 저장
3. 체크포인트: 장기 작업에서 주기적 상태 저장
4. 외부 상태 관리: JSON/DB에 에이전트 상태 저장

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1r1xboo/critical_bug_claude_environment_claude_forgets/
