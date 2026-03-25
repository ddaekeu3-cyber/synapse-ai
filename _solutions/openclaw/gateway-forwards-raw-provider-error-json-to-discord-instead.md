---
layout: solution
title: "Gateway forwards raw provider error JSON to Discord instead of summarizing"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/38581
---

# Gateway forwards raw provider error JSON to Discord instead of summarizing

## 증상
When an LLM provider returns an error payload (e.g. `server_error`), the gateway currently forwards the *raw provider JSON blob* through to Discord as message content (e.g. `Codex error: {...json...}`), instead of masking/summarizing it.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Re-pin the affected session to a different model/provider (e.g. via a `/model <modelRef>` override) to route around the flaky provider until recovery.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38581
