---
layout: solution
title: "Gateway crash-loops on startup: structuredClone FATAL ERROR in orphaned subagent run reconciliation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34539
description: "After the gateway process exits (for any reason), restarting it causes an immediate V8 fatal error in . The gateway cannot recover without manual"
---

# Gateway crash-loops on startup: structuredClone FATAL ERROR in orphaned subagent run reconciliation

## 증상
After the gateway process exits (for any reason), restarting it causes an immediate **V8 fatal error** in `reconcileOrphanedRestoredRuns`. The gateway cannot recover without manual intervention (clearing `~/.openclaw/subagents/runs.json`).

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Clear the orphaned runs manually:
```bash
cp ~/.openclaw/subagents/runs.json ~/.openclaw/subagents/runs.json.bak
echo '{"version":2,"runs":{}}' > ~/.openclaw/subagents/runs.json
rm -rf /tmp/node-compile-cache
sudo systemctl restart openclaw-gateway
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34539
