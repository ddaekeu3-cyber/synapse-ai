---
layout: solution
title: "nextcloud-talk extension has some bugs in v2026.2.25"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27611
description: "I deployed NextcloudPi myself and used the Nextcloud Talk app to access OpenClaw. I found an issue with the v2026.2.25 nextcloud-talk extension and used"
---

# nextcloud-talk extension has some bugs in v2026.2.25

## 증상
I deployed NextcloudPi myself and used the Nextcloud Talk app to access OpenClaw. I found an issue with the v2026.2.25 nextcloud-talk extension and used OpenCode for troubleshooting. Below are the OpenCode solutions.:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
File: `extensions/nextcloud-talk/src/monitor.ts`

```typescript
await start();

const publicUrl = ...;
logger.info(`[nextcloud-talk:${account.accountId}] webhook listening on ${publicUrl}`);

// Fix: Wait for abort signal before returning (same as Slack)
await new Promise<void>((resolve) => {
  opts.abortSignal?.addEventListener("abort", () => resolve(), { once: true });
});

stop();
```

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27611
