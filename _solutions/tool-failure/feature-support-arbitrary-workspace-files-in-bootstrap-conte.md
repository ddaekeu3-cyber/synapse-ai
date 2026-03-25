---
layout: solution
title: "[Feature]: Support arbitrary workspace files in bootstrap context injection"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/39120
---

# [Feature]: Support arbitrary workspace files in bootstrap context injection

## 증상
The bootstrap-extra-files hook only accepts recognized basenames (AGENTS.md, SOUL.md, TOOLS.md, etc.). There's no way to auto-inject custom workspace files into session context at bootstrap time. This forces users to either embed custom context into AGENTS.md (mixing concerns) or spend a tool call reading files at the start of every session (adding latency and cost).

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
https://github.com/openclaw/openclaw/issues/39120
