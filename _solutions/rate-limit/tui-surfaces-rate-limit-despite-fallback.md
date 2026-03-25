---
layout: solution
title: "TUI shows rate limit error even though fallback model succeeds"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/54060
---

# TUI shows rate limit error even though fallback model succeeds

## 증상
After primary model (openai-codex) hits rate limit, fallback model processes request successfully. But TUI still displays rate limit error to user, causing confusion.

## 원인
Error from primary model is surfaced to UI before fallback completes. Error handling does not suppress primary error when fallback succeeds.

## 해결법
### Rate limit 폴백 에러 표시 해결
1. 설정에서 `suppressFallbackErrors: true` 추가 (지원되는 경우)
2. 대안: 기본 모델을 rate limit이 높은 모델로 변경
3. Rate limit 예방: 요청 간 최소 간격 설정
4. 에러가 보이더라도 실제 응답은 정상 — UI 버그로 무시 가능

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54060
