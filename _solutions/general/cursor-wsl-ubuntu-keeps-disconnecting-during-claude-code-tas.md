---
layout: solution
title: "Cursor + WSL Ubuntu keeps disconnecting during Claude Code tasks"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/cursor/comments/1rukxzm/cursor_wsl_ubuntu
---

# Cursor + WSL Ubuntu keeps disconnecting during Claude Code tasks

## 증상
I’m on Windows 11 + WSL Ubuntu, and Cursor keeps becoming unstable specifically when I run Claude Code tasks.

My project is stored in WSL, and the repo itself seems fine:

	•	git status works

	•	project files are still there

	•	I already made a full backup

What happens:

	•	Cursor sometimes opens normally

	•	Remote WSL sometimes connects successfully

	•	but during a Claude Code task it often

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/cursor/comments/1rukxzm/cursor_wsl_ubuntu_keeps_disconnecting_during/
