---
layout: solution
title: "Bug: openclaw-cli cannot connect to gateway when bind=lan on Docker Desktop macOS (Apple Silicon)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49318
description: "Repo:"
---

# Bug: openclaw-cli cannot connect to gateway when bind=lan on Docker Desktop macOS (Apple Silicon)

## 증상
**Repo:** https://github.com/openclaw/openclaw

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Direct file manipulation via `docker exec`:

```bash
docker exec <gateway-container> node -e "
const fs = require('fs');
const crypto = require('crypto');
const pendingPath = '/home/node/.openclaw/devices/pending.json';
const pairedPath  = '/home/node/.openclaw/devices/paired.json';

const pending = JSON.parse(fs.readFileSync(pendingPath, 'utf8'));
let paired = {};
try { paired = JSON.parse(fs.readFileSync(pairedPath, 'utf8')); } catch(e) {}

for (const [reqId, req] of Object.entries(pending)) {
  paired[req.deviceId] = { ...req, approvedAt: Date.now(), approvedBy: 'admin', token: crypto.rando

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49318
