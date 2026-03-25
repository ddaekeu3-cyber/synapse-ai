---
layout: solution
title: "Interactive TUI sessions do not auto-failover on rate_limit; error surfaced to user instead"
category: config
source: https://github.com/openclaw/openclaw/issues/50389
---

# Interactive TUI sessions do not auto-failover on rate_limit; error surfaced to user instead

## 증상
When the primary model (`openai-codex/gpt-5.4`) hits a `rate_limit` error during an **interactive TUI session**, the error is surfaced to the user instead of automatically failing over to the configured fallback chain. The user must manually intervene to switch models.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
1. 공식 문서 참조: 최신 설정 가이드 확인
2. 환경변수 확인: 필수 변수 설정 확인
3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인
4. 로그 확인: 시작 로그에서 설정 관련 경고 확인
5. 최소 설정으로 시작해서 하나씩 추가

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50389
