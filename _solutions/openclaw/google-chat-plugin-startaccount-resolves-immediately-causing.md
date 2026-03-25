---
layout: solution
title: "Google Chat plugin: startAccount resolves immediately, causing infinite restart loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/20502
---

# Google Chat plugin: startAccount resolves immediately, causing infinite restart loop

## 증상
The Google Chat plugin's `startAccount` function in `extensions/googlechat/src/channel.ts` resolves immediately after registering the webhook handler via `startGoogleChatMonitor()`. Since Google Chat is webhook-based (no persistent connection like WhatsApp's WebSocket), the async function returns right away.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
The `startAccount` function should await the abort signal before returning, keeping the promise pending:

```typescript
// In extensions/googlechat/src/channel.ts, startAccount:
const unregister = await startGoogleChatMonitor({...});

// Keep the promise pending until the abort signal fires
await new Promise<void>((resolve) => {
  if (ctx.abortSignal.aborted) { resolve(); return; }
  ctx.abortSignal.addEventListener('abort', () => resolve(), { once: true });
});

unregister?.();
ctx.setStatus({ accountId: account.accountId, running: false, lastStopAt: Date.now() });
```

This pattern ensures t

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20502
