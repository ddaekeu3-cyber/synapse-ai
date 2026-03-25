---
layout: solution
title: "nextcloud-talk plugin: EADDRINUSE crash loop due to premature startAccount promise resolution"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/19854
---

# nextcloud-talk plugin: EADDRINUSE crash loop due to premature startAccount promise resolution

## 증상
The `nextcloud-talk` plugin's `startAccount` function resolves immediately after starting the webhook HTTP server. The gateway framework's channel lifecycle manager interprets a resolved `startAccount` promise as "channel exited" and triggers auto-restart. The restart attempt tries to bind the same webhook port (default 8788) while the first server is still listening, causing `EADDRINUSE` which cr

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Patch `extensions/nextcloud-talk/src/channel.ts` to await the abort signal:

```typescript
startAccount: async (ctx) => {
  // ...
  const { stop } = await monitorNextcloudTalkProvider({...});

  // Keep the promise pending until the channel is stopped
  await new Promise<void>((resolve) => {
    if (ctx.abortSignal?.aborted) { resolve(); return; }
    ctx.abortSignal?.addEventListener("abort", () => resolve(), { once: true });
  });

  return { stop };
},
```

This prevents the framework from treating the webhook server as "exited" and avoids the EADDRINUSE crash loop.

**Note:** This patch i

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19854
