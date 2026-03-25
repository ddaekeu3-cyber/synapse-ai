---
layout: solution
title: "docs+fix: script cron jobs have undocumented 10-min default timeout; timeoutSeconds not documented for script payload"
category: performance
source: https://github.com/openclaw/openclaw/issues/52168
---

# docs+fix: script cron jobs have undocumented 10-min default timeout; timeoutSeconds not documented for script payload

## 증상
`cron` jobs with `payload.kind = "script"` have a hardcoded default timeout of **600 seconds (10 minutes)**. This default is not documented anywhere. `timeoutSeconds` is listed in the docs only for `agentTurn` payloads, leaving `script` job authors with no guidance.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
it and may not realize the issue is a timeout since the error message is generic.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52168
