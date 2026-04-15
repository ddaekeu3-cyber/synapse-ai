---
layout: solution
title: "Synology Chat plugin: sendText/sendMedia fails with 'incoming URL not configured' due to empty config fallback"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37852
description: "The and functions in the Synology Chat plugin () fail when called by the delivery recovery system because they fall back to with an empty config object"
---

# Synology Chat plugin: sendText/sendMedia fails with 'incoming URL not configured' due to empty config fallback

## 증상
The `sendText` and `sendMedia` functions in the Synology Chat plugin (`extensions/synology-chat/src/channel.ts`) fail when called by the delivery recovery system because they fall back to `resolveAccount({}, accountId)` with an empty config object `{}`.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Load the real config from runtime when `ctxAccount` is not provided:

```diff
  sendText: async ({ to, text, accountId, account: ctxAccount }: any) => {
-   const account = ctxAccount ?? resolveAccount({}, accountId);
+   let account;
+   if (ctxAccount) {
+     account = ctxAccount;
+   } else {
+     const rt = getSynologyRuntime();
+     const currentCfg = await rt.config.loadConfig();
+     account = resolveAccount(currentCfg, accountId);
+   }
```

Same fix for `sendMedia`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37852
