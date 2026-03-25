---
layout: solution
title: "Cron isolated sessions lose sandbox.docker.dangerouslyAllow* flags due to shallow Object.assign on agents.defaults"
category: docker
source: https://github.com/openclaw/openclaw/issues/38067
---

# Cron isolated sessions lose sandbox.docker.dangerouslyAllow* flags due to shallow Object.assign on agents.defaults

## 증상
`runCronIsolatedAgentTurn()` in `src/cron/isolated-agent/run.ts` builds a modified config by shallow-merging the per-agent config on top of `agents.defaults`:

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Set `dangerouslyAllowExternalBindSources: true` explicitly in the per-agent `sandbox.docker` config (not just in defaults).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38067
