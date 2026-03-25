---
layout: solution
title: "Claude Code CLI unresponsive for ~5 minutes after laptop resume from suspend (Linux)"
category: performance
source: https://github.com/anthropics/claude-code/issues/32116
---

# Claude Code CLI unresponsive for ~5 minutes after laptop resume from suspend (Linux)

## 증상
After resuming from s2idle suspend on Linux, Claude Code CLI becomes unresponsive for approximately 5 minutes before it can process new prompts. The delay is caused by the CLI sending an API request on a stale TCP connection (which silently succeeds into the kernel buffer), then waiting ~4 minutes for a response that will never come before eventually reconnecting.

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
https://github.com/anthropics/claude-code/issues/32116
