---
layout: solution
title: "Gateway holds port after npm upgrade — new process crash-loops until openclaw gateway stop is run"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44881
description: "When upgrading OpenClaw via , the old gateway process continues to hold the WebSocket port () even after the binary is"
---

# Gateway holds port after npm upgrade — new process crash-loops until openclaw gateway stop is run

## 증상
When upgrading OpenClaw via `npm install -g openclaw@latest`, the old gateway process continues to hold the WebSocket port (`ws://127.0.0.1:18789`) even after the binary is replaced.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
```bash
openclaw gateway stop   # must use this — regular kill is not enough
npm install -g openclaw@latest
pm2 restart potebot
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44881
