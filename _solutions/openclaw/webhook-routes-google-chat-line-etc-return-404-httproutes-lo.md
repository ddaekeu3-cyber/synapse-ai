---
layout: solution
title: "Webhook routes (Google Chat, LINE, etc.) return 404 — httpRoutes lost on plugin registry swap (v2026.3.12)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45445
description: "<!-- ~/.openclaw/GITHUB-ISSUE-BODY.md"
---

# Webhook routes (Google Chat, LINE, etc.) return 404 — httpRoutes lost on plugin registry swap (v2026.3.12)

## 증상
<!-- ~/.openclaw/GITHUB-ISSUE-BODY.md -->

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We applied a local patch to the dist files covering both issues:

1. **gateway-cli-\*.js** (2 files): Changed `createGatewayPluginRequestHandler` to resolve `getActivePluginRegistry() ?? _initialRegistry` on each request. Also patched `shouldEnforcePluginGatewayAuth` similarly.

2. **registry-\*.js** (2 files in dist root): Added a `Object.defineProperty` interceptor on the global registry state's `registry` property that copies `httpRoutes` from old → new on every swap. The `_httpRoutePatchApplied` guard ensures it installs once despite multiple modules reading the same global.

Both patches 

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45445
