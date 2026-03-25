---
layout: solution
title: "Telegram heartbeat loop: agent sends message bursts every ~10min instead of HEARTBEAT_OK"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37136
---

# Telegram heartbeat loop: agent sends message bursts every ~10min instead of HEARTBEAT_OK

## 증상
**OS:** macOS (Darwin 24.6.0, arm64)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Gateway restart clears the loop (kills all sessions). But it recurs on next heartbeat cycle.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37136
