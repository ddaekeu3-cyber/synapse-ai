---
layout: solution
title: "Telegram group messages not delivered to agent despite correct groupPolicy/groups config"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53419
---

# Telegram group messages not delivered to agent despite correct groupPolicy/groups config

## 증상
Telegram group messages are not reaching the agent even when:

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
fixing config format)

## Expected behaviour

Messages from the group chat ID listed under `channels.telegram.groups` should be processed by the agent.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53419
