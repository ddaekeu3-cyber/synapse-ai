---
layout: solution
title: "Google Gemini Provider: 429 Rate Limit Blocks Entire Provider Instead of Specific Model"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/26103
---

# Google Gemini Provider: 429 Rate Limit Blocks Entire Provider Instead of Specific Model

## 증상
When using Google Gemini models, if one specific model (e.g., `gemini-3.1-pro-preview-customtools`) hits a rate limit (429), the OpenClaw gateway appears to block/backoff the entire `google` provider. This prevents fallback or manual switching to other available models (e.g., `gemini-3.0-pro-preview`) under the same provider, which likely have independent quotas or are not yet rate-limited.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
1. 지수 백오프: 1초→2초→4초→8초 재시도 간격
2. 지터 추가: 랜덤 지터로 thundering herd 방지
3. 캐싱: 동일 요청 결과 캐싱
4. Retry-After 헤더 준수
5. 배치 처리: 개별 요청을 배치로 묶기

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26103
