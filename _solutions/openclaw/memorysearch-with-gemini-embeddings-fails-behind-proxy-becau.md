---
layout: solution
title: "memory_search with Gemini embeddings fails behind proxy because memory remote fetch ignores env proxy (fetchWithSsrFGuard strict mode)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46934
---

# memory_search with Gemini embeddings fails behind proxy because memory remote fetch ignores env proxy (fetchWithSsrFGuard strict mode)

## 증상
`memory_search` fails with Gemini embeddings in OpenClaw 2026.3.8 when the host requires an HTTP/HTTPS proxy for outbound access.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
No reliable workaround yet besides patching OpenClaw locally or avoiding this remote embedding path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46934
