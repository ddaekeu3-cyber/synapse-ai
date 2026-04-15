---
layout: solution
title: "cron: script payload timeoutSeconds not enforced"
category: performance
source: https://github.com/openclaw/openclaw/issues/47608
description: "is defined in the type but never applied — script jobs always run with the (10 min) ceiling regardless of what is set"
---

# cron: script payload timeoutSeconds not enforced

## 증상
`CronScriptPayload.timeoutSeconds` is defined in the type but never applied — script jobs always run with the `DEFAULT_JOB_TIMEOUT_MS` (10 min) ceiling regardless of what `timeoutSeconds` is set to.

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Extend the condition to cover `script` in addition to `agentTurn`:

\`\`\`ts
const configuredTimeoutMs =
  (job.payload.kind === "agentTurn" || job.payload.kind === "script") &&
  typeof job.payload.timeoutSeconds === "number"
    ? Math.floor(job.payload.timeoutSeconds * 1_000)
    : undefined;
\`\`\`

Also fix `makeJob` test helper in `timeout-policy.test.ts` which defaults `sessionTarget` to `"main"` for non-agentTurn payloads — script jobs require `"isolated"`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47608
