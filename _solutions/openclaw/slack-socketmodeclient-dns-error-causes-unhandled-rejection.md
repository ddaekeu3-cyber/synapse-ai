---
layout: solution
title: "Slack SocketModeClient DNS error causes unhandled rejection → process crash"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/21082
---

# Slack SocketModeClient DNS error causes unhandled rejection → process crash

## 증상
The OpenClaw gateway process terminates when the Slack `SocketModeClient` encounters a DNS resolution failure (`getaddrinfo ENOTFOUND slack.com`). The error propagates as an unhandled promise rejection, which kills the entire Node.js process.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using `launchctl` / `KeepAlive` to auto-restart the gateway after crash. This limits downtime but doesn't prevent data loss (e.g., in-flight session logs).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/21082
