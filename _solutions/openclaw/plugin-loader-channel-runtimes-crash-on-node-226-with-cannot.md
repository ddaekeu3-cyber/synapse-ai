---
layout: solution
title: "Plugin loader: channel runtimes crash on Node 22.6+ with 'Cannot find module '…manager.js''"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49744
description: "Channel runtimes (Discord voice, WhatsApp, and any extension that lazy-loads a file via dynamic ) crash on every startup on Node 22.6 and above, including"
---

# Plugin loader: channel runtimes crash on Node 22.6+ with "Cannot find module '…manager.js'"

## 증상
Channel runtimes (Discord voice, WhatsApp, and any extension that lazy-loads a `.runtime.ts` file via dynamic `import()`) crash on every startup on **Node 22.6 and above**, including all current Node 22 LTS releases.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Until fixed upstream, apply the one-line patch locally after cloning:

```
// src/plugins/loader.ts, inside buildPluginLoaderJitiOptions:
tryNative: false,
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49744
