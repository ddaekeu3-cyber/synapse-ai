---
layout: solution
title: "LINE plugin webhook route not registered in gateway HTTP handler (registry mismatch)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49803
---

# LINE plugin webhook route not registered in gateway HTTP handler (registry mismatch)

## 증상
**OpenClaw version:** 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Building a standalone webhook relay server that receives LINE webhooks and forwards messages into the agent session via alternative means.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49803
