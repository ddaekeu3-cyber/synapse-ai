---
layout: solution
title: "Browser tool: zombie Chrome process blocks CDP port, causes gateway timeouts"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41750
description: "When the OpenClaw gateway launches a Chrome instance for the browser tool (via ), the Chrome process can become unresponsive (zombie state) while still"
---

# Browser tool: zombie Chrome process blocks CDP port, causes gateway timeouts

## 증상
When the OpenClaw gateway launches a Chrome instance for the browser tool (via `--remote-debugging-port=18800`), the Chrome process can become unresponsive (zombie state) while still holding the CDP port. Subsequent browser tool calls find port 18800 occupied but unresponsive, causing repeated timeouts.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually kill the zombie Chrome process:
```bash
kill $(ps aux | grep "remote-debugging-port=18800" | grep -v grep | awk "{print \$2}")
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41750
