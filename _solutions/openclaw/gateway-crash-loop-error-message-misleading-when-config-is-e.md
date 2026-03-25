---
layout: solution
title: "Gateway crash-loop error message misleading when config is empty/incomplete after migration"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54201
---

# Gateway crash-loop error message misleading when config is empty/incomplete after migration

## 증상
When the gateway starts with an incomplete or effectively empty config (e.g. after a failed migration), it crash-loops every ~10 seconds with a message that points at a symptom rather than the root cause, making diagnosis very difficult.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
was not setting `gateway.mode` — it was merging the full config from `moltbot.json` into `openclaw.json`. Adding `gateway.mode=local` alone (the literal fix suggested by the error) would have started the gateway but left all channels, agents, bindings, and plugins missing, with no further errors or warnings.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54201
