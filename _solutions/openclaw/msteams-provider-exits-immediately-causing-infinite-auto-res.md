---
layout: solution
title: "msteams provider exits immediately, causing infinite auto-restart loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27885
description: "in returns immediately after starting the Express server. The channel manager in treats promise resolution as \"provider stopped\" and triggers auto-restart"
---

# msteams provider exits immediately, causing infinite auto-restart loop

## 증상
`monitorMSTeamsProvider()` in `extensions/msteams/src/monitor.ts` returns immediately after starting the Express server. The channel manager in `src/gateway/server-channels.ts` treats promise resolution as "provider stopped" and triggers auto-restart with exponential backoff, resulting in an infinite restart loop:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Add the same blocking pattern before the return in `extensions/msteams/src/monitor.ts`:

```typescript
// Block until abort signal fires — the channel manager treats promise
// resolution as "provider stopped" and triggers auto-restart.
await new Promise<void>((resolve) => {
  if (opts.abortSignal?.aborted) {
    resolve();
    return;
  }
  opts.abortSignal?.addEventListener("abort", () => resolve(), { once: true });
});
```

Also add `{ once: true }` to the existing abort listener to prevent leaks:

```typescript
if (opts.abortSignal) {
  opts.abortSignal.addEventListener("abort", () => {
  

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27885
