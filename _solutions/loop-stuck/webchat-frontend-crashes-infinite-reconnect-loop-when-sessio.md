---
layout: solution
title: "Webchat frontend crashes (infinite reconnect loop) when session history is large"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/27769
---

# Webchat frontend crashes (infinite reconnect loop) when session history is large

## 증상
The webchat frontend enters an infinite disconnect/reconnect loop when a session has a large chat history, making it appear as if there is a network connectivity issue.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Opening a new session (e.g. `?session=test`) works normally, confirming the issue is tied to session history size, not network.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27769
