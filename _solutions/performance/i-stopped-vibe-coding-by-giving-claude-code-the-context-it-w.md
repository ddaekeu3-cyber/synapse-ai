---
layout: solution
title: "I stopped vibe coding by giving Claude Code the context it was missing: reverse maps + forward specs + MCP"
category: performance
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1ngxlqg/i_stopped_vib
---

# I stopped vibe coding by giving Claude Code the context it was missing: reverse maps + forward specs + MCP

## 증상
**Claude code isn't dumb, it's context-starved.**

Here's what happened:I was thrown into a project I knew nothing about ... a sprawling codebase with years of accumulated business logic, edge cases, and interconnected systems. I had to add a feature that a client had requested. Claude Code analyzed the files I showed it and suggested what looked like clean, elegant code. I trusted it.

Then came 

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
1. 병목 식별: 프로파일링으로 가장 느린 부분 찾기
2. 캐싱: 반복 연산/API 호출 캐싱
3. 병렬 처리: 독립 작업 동시 실행
4. 타임아웃 설정: 무한 대기 방지
5. 리소스 모니터링: CPU, 메모리, 네트워크 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1ngxlqg/i_stopped_vibe_coding_by_giving_claude_code_the/
