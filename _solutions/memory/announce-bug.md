---
layout: solution
title: "announce bug"
category: memory
source: https://github.com/openclaw/openclaw/issues/48966
---

# announce bug

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #48966에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Agents can reply `ANNOUNCE_SKIP` during the announce step to stay silent, but this doesn't prevent the message from being sent to the main agent in the first place.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48966
