---
layout: solution
title: "OOM crash: loadCombinedSessionStoreForGateway loads all agent session stores simultaneously"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51264
---

# OOM crash: loadCombinedSessionStoreForGateway loads all agent session stores simultaneously

## 증상
OpenClaw's gateway crashes with an out-of-memory (OOM) error when `loadCombinedSessionStoreForGateway` is called during hook dispatch. The function eagerly loads **every agent's full `sessions.json`** into memory simultaneously — even when only a single agent's session store is needed. Under production load with 10+ agents, this causes Node.js heap exhaustion.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Until a fix is shipped, periodically prune old hook sessions from each agent's `sessions.json`. Any session with an ID matching `hook:atlas:jobrun:*` (or equivalent) that is in a terminal state (completed, failed, cancelled) can be safely deleted.

We implemented a periodic pruning script that runs against all agent session files to remove completed hook sessions, which resolved the OOM crashes in our deployment.

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51264
