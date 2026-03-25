---
layout: solution
title: "SSE streaming hangs indefinitely (no timeout) + ESC cannot fully cancel (queue auto-restart) — root cause analysis with fix proposals"
category: performance
source: https://github.com/anthropics/claude-code/issues/33949
---

# SSE streaming hangs indefinitely (no timeout) + ESC cannot fully cancel (queue auto-restart) — root cause analysis with fix proposals

## 증상
These two bugs have been plaguing users for months (see #26224 — 28 comments, #6836 — 150+ reports), with no root cause analysis from the team. After yet another day of babysitting Claude Code and pressing ESC every few minutes to revive a hung agent, we decided to conduct our own deep investigation — reverse-engineering `cli.js` across 12 npm package versions and analyzing 1,571 session JSONL fil

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
https://github.com/anthropics/claude-code/issues/33949
