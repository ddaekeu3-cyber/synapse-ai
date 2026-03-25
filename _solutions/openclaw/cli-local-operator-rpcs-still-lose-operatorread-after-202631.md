---
layout: solution
title: "[CLI] Local operator RPCs still lose operator.read after 2026.3.12 shared-auth hardening"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52338
---

# [CLI] Local operator RPCs still lose operator.read after 2026.3.12 shared-auth hardening

## 증상
On a Linux loopback-only gateway running OpenClaw `2026.3.13`, several local CLI/operator RPC paths still fail with `missing scope: operator.read` or WebSocket close `1000 normal closure`, even though the local paired device token was explicitly repaired to include both `operator.read` and `operator.admin`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
On this host I currently have to:

- trust `openclaw gateway status --json --url ... --token ...` for operator health
- read cron state directly from `~/.openclaw/cron/jobs.json` and `~/.openclaw/cron/runs/*.jsonl`
- treat token-explicit `browser status` as flaky even though actual `browser.request` works

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52338
