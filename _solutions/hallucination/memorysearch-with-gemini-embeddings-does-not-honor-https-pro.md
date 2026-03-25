---
layout: solution
title: "memory_search with Gemini embeddings does not honor HTTP(S) proxy in 2026.3.23-2"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/54279
---

# memory_search with Gemini embeddings does not honor HTTP(S) proxy in 2026.3.23-2

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
A local patch to the memory remote HTTP helper fixes the issue.

After patching the memory fetch path to use the env-proxy-aware guarded fetch helper and restarting the gateway:

- openclaw memory status --agent main --deep shows Embeddings: ready
- openclaw memory search ... returns normal results
- in-app memory_search works again

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54279
