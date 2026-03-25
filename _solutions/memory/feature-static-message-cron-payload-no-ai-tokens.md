---
layout: solution
title: "Feature: Static message cron payload (no AI tokens)"
category: memory
source: https://github.com/openclaw/openclaw/issues/11473
---

# Feature: Static message cron payload (no AI tokens)

## 증상
Currently, all cron payloads require an AI call to process:

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
is to use `agentTurn` with a shell script that curls the Telegram API directly, but that's clunky and still requires an AI call to trigger it.

---
*Requested by Roel via Daniella*

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/11473
