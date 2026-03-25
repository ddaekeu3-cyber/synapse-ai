---
layout: solution
title: "Google provider authHeader:true returns 400 Missing Authentication"
category: auth
source: https://github.com/openclaw/openclaw/issues/54175
---

# Google provider authHeader:true returns 400 Missing Authentication

## 증상
Setting `authHeader: true` in Google provider config causes all API calls to return 400 "Missing or invalid Authentication". Standard API key authentication fails.

## 원인
The authHeader flag is not correctly injecting the Authorization header for Google API requests. The provider implementation may expect a different header format.

## 해결법
### Google Provider 인증 실패 해결
1. `authHeader: true` 대신 API 키를 query parameter로 전달
2. 설정에서 `authHeader` 제거하고 `apiKey` 직접 지정
3. Google API는 `Authorization: Bearer` 대신 `x-goog-api-key` 헤더 사용
4. 환경변수 확인: `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY` 설정

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54175
