---
layout: solution
title: "ReferenceError: _skey is not defined — cron jobs crash intermittently"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49054
---

# ReferenceError: _skey is not defined — cron jobs crash intermittently

## 증상
Cron-triggered sessions (both `agentTurn` isolated and `systemEvent` main) intermittently crash with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None known. Setting `delivery: "none"` does not prevent the error.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49054
