---
layout: solution
title: "WebSocket handshake timeout too aggressive for lower-powered hardware"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48168
description: "The gateway WebSocket handshake timeout () is too short for lower-powered hardware where Node.js module loading is slow, causing CLI commands that require"
---

# WebSocket handshake timeout too aggressive for lower-powered hardware

## 증상
The gateway WebSocket handshake timeout (`DEFAULT_HANDSHAKE_TIMEOUT_MS = 3000`) is too short for lower-powered hardware where Node.js module loading is slow, causing CLI commands that require a gateway connection (`cron list`, `cron status`, `agent`, etc.) to fail intermittently or consistently with:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Patching `DEFAULT_HANDSHAKE_TIMEOUT_MS` from `3e3` to `10e3` in `gateway-cli-*.js` resolves the issue completely. This patch survives until the next `npm install -g openclaw`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48168
