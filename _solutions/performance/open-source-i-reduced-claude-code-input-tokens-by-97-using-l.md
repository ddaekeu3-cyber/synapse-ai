---
layout: solution
title: "[Open Source] I reduced Claude Code input tokens by 97% using local semantic search (Benchmark vs Grep)"
category: performance
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qiv0d3/open_source_i_r
---

# [Open Source] I reduced Claude Code input tokens by 97% using local semantic search (Benchmark vs Grep)

## 증상
Hi r/ClaudeAI,

Since the release of **Claude Code**, I’ve been using it extensively. However, I quickly noticed a major bottleneck when working on large codebases: token consumption explodes whenever you ask the agent to explore the project structure.

The culprit is the reliance on basic tools like `grep` or `glob` for file discovery. To find relevant code, Claude often has to:

1. List dozens o

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qiv0d3/open_source_i_reduced_claude_code_input_tokens_by/
