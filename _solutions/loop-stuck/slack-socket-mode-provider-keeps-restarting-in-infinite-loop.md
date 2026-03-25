---
layout: solution
title: "Slack Socket Mode: Provider keeps restarting in infinite loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/32739
---

# Slack Socket Mode: Provider keeps restarting in infinite loop

## 증상
Slack provider with Socket Mode enabled keeps restarting every 5-40 seconds in an infinite loop. No error messages are logged.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Using Cron job to poll conversations.history every 60 seconds as fallback.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32739
