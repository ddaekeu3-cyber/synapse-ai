---
layout: solution
title: "MCP proxy silently hangs on tool_use results — Claude CLI blocks waiting for complete tool_result set"
category: performance
source: https://github.com/anthropics/claude-code/issues/38437
---

# MCP proxy silently hangs on tool_use results — Claude CLI blocks waiting for complete tool_result set

## 증상
When using `claude -p` (headless mode) with claude.ai MCP servers, the Anthropic MCP proxy silently drops tool results. The upstream MCP server (Bear Notes) responds successfully (HTTP 200, ~44ms), but the proxy never delivers the result back to Claude CLI. Since Claude CLI requires the **complete set of tool_results** before continuing, this causes it to block indefinitely until the inactivity ti

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
https://github.com/anthropics/claude-code/issues/38437
