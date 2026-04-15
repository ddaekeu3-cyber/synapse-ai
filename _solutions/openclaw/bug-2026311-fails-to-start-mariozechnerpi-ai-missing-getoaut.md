---
layout: solution
title: "Bug: 2026.3.11 fails to start — @mariozechner/pi-ai missing getOAuthApiKey / getEnvApiKey exports from main index"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43753
description: "- OpenClaw: 2026.3.11 (from source, git tag"
---

# Bug: 2026.3.11 fails to start — @mariozechner/pi-ai missing getOAuthApiKey / getEnvApiKey exports from main index

## 증상
- OpenClaw: 2026.3.11 (from source, git tag `v2026.3.11`)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually patching `node_modules/@mariozechner/pi-ai/dist/index.js` to add:

```js
export * from "./utils/oauth/index.js";
```

restores normal operation. This is a node_modules patch so it will be lost on reinstall.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43753
