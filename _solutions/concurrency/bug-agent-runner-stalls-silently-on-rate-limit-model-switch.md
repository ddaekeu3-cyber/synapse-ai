---
layout: solution
title: "bug: agent runner stalls silently on rate limit / model switch mid-stream (attempt.ts race condition)"
category: concurrency
source: https://github.com/openclaw/openclaw/issues/31664
---

# bug: agent runner stalls silently on rate limit / model switch mid-stream (attempt.ts race condition)

## 증상
When an API rate limit or model fallback (e.g. Claude → Gemini) occurs mid-stream, the agent state machine in `pi-embedded-runner/run/attempt.ts` can stall permanently with no error surfaced to the user. The session goes silent and never resumes, even after the rate limit clears.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
1. 락 사용: 공유 리소스에 적절한 락/뮤텍스
2. 원자적 연산: 경쟁 조건 방지
3. 큐 기반 처리: 메시지 큐로 통신
4. 타임아웃: 락 대기에 타임아웃 설정
5. 스트레스 테스트: 동시성 버그 발견

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31664
