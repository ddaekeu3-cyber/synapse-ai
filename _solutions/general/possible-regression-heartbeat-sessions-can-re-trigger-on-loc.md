---
layout: solution
title: "Possible regression: heartbeat sessions can re-trigger on local exec completed events and spam duplicate heartbeat log entries"
category: general
source: https://github.com/openclaw/openclaw/issues/46798
---

# Possible regression: heartbeat sessions can re-trigger on local exec completed events and spam duplicate heartbeat log entries

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
area is somewhere in the overlap of:
local exec completion event classification
wake/session-event routing
sessionKey propagation
heartbeat prompt selection

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46798
