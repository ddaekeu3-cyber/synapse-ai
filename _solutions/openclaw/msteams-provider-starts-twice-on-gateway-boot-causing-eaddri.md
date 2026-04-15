---
layout: solution
title: "msteams provider starts twice on gateway boot, causing EADDRINUSE restart loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/22169
description: "- OpenClaw: 2026.2.19-2"
---

# msteams provider starts twice on gateway boot, causing EADDRINUSE restart loop

## 증상
- OpenClaw: 2026.2.19-2 (45d9b20)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
We patched `monitor.ts` locally to detect EADDRINUSE and skip duplicate binds:

```typescript
httpServer.on("error", (err) => {
    const code = (err as any).code;
    if (code === "EADDRINUSE") {
        log.warn(`msteams port ${port} already in use by another provider instance — skipping duplicate bind`);
        return;
    }
    log.error(`msteams server error: ${String(err)} [code=${code}]`);
});

await new Promise<void>((resolve) => {
    httpServer.listen(port, () => {
        log.info(`msteams provider started on port ${port}`);
        resolve();
    });
    httpServer.once("error", (

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22169
