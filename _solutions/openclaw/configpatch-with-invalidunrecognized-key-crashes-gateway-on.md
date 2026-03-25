---
layout: solution
title: "config.patch with invalid/unrecognized key crashes gateway on every restart"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40265
---

# config.patch with invalid/unrecognized key crashes gateway on every restart

## 증상
When `config.patch` is called with an invalid or unrecognized configuration key (e.g., `gateway.hooks`), the gateway writes the invalid config and then crashes on restart. Because the invalid config persists on disk, every subsequent restart attempt also crashes, requiring **manual config file editing** to recover.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Always call `config.schema` first to validate keys before using `config.patch`. We added this as a hard rule in our operational runbook.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40265
