---
layout: solution
title: "Slack WebSocket 408 timeout causes unhandled promise rejection + gateway crash"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45852
description: "The OpenClaw gateway crashes when the Slack Socket Mode WebSocket connection receives a 408 (Request Timeout) response. An unhandled promise rejection is"
---

# Slack WebSocket 408 timeout causes unhandled promise rejection + gateway crash

## 증상
The OpenClaw gateway crashes when the Slack Socket Mode WebSocket connection receives a 408 (Request Timeout) response. An unhandled promise rejection is thrown and the Node.js process exits.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Wrapping `openclaw-gateway` in a watchdog loop (`while true; do openclaw-gateway; sleep 5; done`) provides auto-restart, but the root cause (unhandled rejection in Slack WebSocket error handler) should be fixed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45852
