---
layout: solution
title: "Session hangs indefinitely when compaction times out, causing repeated duplicate message sends"
category: performance
source: https://github.com/openclaw/openclaw/issues/43661
---

# Session hangs indefinitely when compaction times out, causing repeated duplicate message sends

## 증상
When a session context becomes large enough to trigger compaction, and that compaction process times out, the agent enters a silent failure loop. Each timeout (~10 min) triggers a delivery retry that resends the same message to the user repeatedly — with no recovery, no fallback, and no way for the session to self-resolve.

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
https://github.com/openclaw/openclaw/issues/43661
