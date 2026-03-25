---
layout: solution
title: "sessions_send announce retry blocks agent session for ~6 minutes on channel errors"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53204
---

# sessions_send announce retry blocks agent session for ~6 minutes on channel errors

## 증상
When an agent uses `sessions_send` with `timeoutSeconds > 0` to query another agent, and the announce step fails (e.g. after a gateway restart when channels are temporarily unavailable), the **announce retry loop blocks the entire agent session** for several minutes.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `timeoutSeconds: 0` (fire-and-forget) + `sessions_history` to read the response avoids the announce step entirely, but loses the synchronous request-reply convenience.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53204
