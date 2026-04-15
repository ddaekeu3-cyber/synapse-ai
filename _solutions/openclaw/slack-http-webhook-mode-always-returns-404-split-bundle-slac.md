---
layout: solution
title: "Slack HTTP webhook mode always returns 404 (split-bundle slackHttpRoutes Map bug)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46246
description: "Version: 2026.3.13"
---

# Slack HTTP webhook mode always returns 404 (split-bundle slackHttpRoutes Map bug)

## 증상
**Version:** 2026.3.13 (61d171a)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Patch all bundle files containing `const slackHttpRoutes = /* @__PURE__ */ new Map();` to share a single global instance:

```js
// Before:
const slackHttpRoutes = /* @__PURE__ */ new Map();

// After:
global.__openclawSlackRoutes ??= new Map();
const slackHttpRoutes = global.__openclawSlackRoutes;
```

This can be scripted as an `ExecStartPre` in the systemd service to auto-apply after every update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46246
