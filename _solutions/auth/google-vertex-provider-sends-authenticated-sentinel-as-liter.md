---
layout: solution
title: "google-vertex provider sends authenticated sentinel as literal API key, causing 401 on Vertex AI"
category: auth
source: https://github.com/openclaw/openclaw/issues/49039
---

# google-vertex provider sends authenticated sentinel as literal API key, causing 401 on Vertex AI

## 증상
2026.3.13 (pi-ai 0.58.0 bundled)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Patch node_modules/@mariozechner/pi-ai/dist/providers/google-vertex.js resolveApiKey to filter out the ADC sentinel value and return undefined instead, allowing the SDK to fall through to ADC auth.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49039
