---
layout: solution
title: "No circuit breaker on corrupted session history — infinite retry death spiral"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/25993
---

# No circuit breaker on corrupted session history — infinite retry death spiral

## 증상
When a session's conversation history becomes corrupted (mismatched `tool_use`/`tool_result` pairs, typically from a timeout interrupting a tool call mid-stream), the gateway retries the same broken history indefinitely. Every retry sends the corrupted context to the API, which either rejects it or times out, creating a death spiral.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
1. 에러 메시지 정확히 읽기: 에러 코드로 원인 파악
2. 권한 확인: API 키, 토큰, 스코프 확인
3. 버전 호환성: 도구/API 버전 확인
4. 대체 도구: 실패 시 동일 기능의 대체 도구 사용
5. 재시도: 일시적 오류는 지수 백오프로 재시도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25993
