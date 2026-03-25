---
layout: solution
title: "memory-lancedb: Ollama embedding returns wrong dimensions due to base64 encoding"
category: general
source: https://github.com/openclaw/openclaw/issues/45982
---

# memory-lancedb: Ollama embedding returns wrong dimensions due to base64 encoding

## 증상
When using Ollama as the embedding provider for `memory-lancedb`, vector search fails with dimension mismatch error:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Manually edit the plugin file to add `encoding_format: "float"` (needs to be reapplied after updates).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45982
