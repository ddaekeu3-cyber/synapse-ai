---
layout: solution
title: "message:sent internal hook not firing for Telegram group deliveries (missing sessionKey)"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/52390
---

# message:sent internal hook not firing for Telegram group deliveries (missing sessionKey)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
Using send.sh scripts in cron jobs to manually duplicate messages to other platforms. No workaround exists for real-time bot replies in group chats.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52390
