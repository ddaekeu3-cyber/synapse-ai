---
layout: solution
title: "CLI crashes with 'CLI output was not valid JSON' when Bedrock /count-tokens returns 403"
category: auth
source: https://github.com/anthropics/claude-code/issues/23519
---

# CLI crashes with "CLI output was not valid JSON" when Bedrock /count-tokens returns 403

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Intercept `/model/{modelId}/count-tokens` requests at the proxy layer and return a synthetic `{"input_tokens": <estimate>}` response before the request reaches Bedrock.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/23519
