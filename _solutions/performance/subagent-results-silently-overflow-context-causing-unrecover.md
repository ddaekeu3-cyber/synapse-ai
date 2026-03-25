---
layout: solution
title: "Subagent results silently overflow context, causing unrecoverable session crash"
category: performance
source: https://github.com/anthropics/claude-code/issues/23463
---

# Subagent results silently overflow context, causing unrecoverable session crash

## 증상
3 When multiple Task tool subagents complete and return large results to the parent agent, the combined results can overflow the parent's context window. This causes the parent to enter a terminal "Prompt is too long" loop — it cannot process any incoming messages, cannot summarize or act on the results, and the session becomes permanently unresponsive until the user force-quits.

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
https://github.com/anthropics/claude-code/issues/23463
