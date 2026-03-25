---
layout: solution
title: "[Feature]: Add gateway:shutdown hook event"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50539
---

# [Feature]: Add gateway:shutdown hook event

## 증상
Currently, OpenClaw hooks only support `gateway:startup` event for gateway lifecycle. There is no corresponding `gateway:shutdown` event to notify users when the gateway is stopping.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None available. The hook system doesn't support shutdown events.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50539
