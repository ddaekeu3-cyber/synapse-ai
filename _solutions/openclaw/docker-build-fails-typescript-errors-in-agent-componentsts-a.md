---
layout: solution
title: "Docker build fails — TypeScript errors in agent-components.ts and pairing-store.ts after commit 3a08e69"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32204
---

# Docker build fails — TypeScript errors in agent-components.ts and pairing-store.ts after commit 3a08e69

## 증상
Docker build (`pnpm build`) fails during the `build:plugin-sdk:dts` step with TypeScript errors in `src/discord/monitor/agent-components.ts` and `src/pairing/pairing-store.ts` introduced in commit `3a08e69` (_refactor: unify queueing and normalize telegram slack flows_).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
adding `NODE_OPTIONS=--max-old-space-size=400` as an environment variable — may be worth documenting in Render deployment docs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32204
