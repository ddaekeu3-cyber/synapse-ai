---
layout: solution
title: "Reload watcher triggers infinite restart loop for npm-installed plugins"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/41001
description: "The gateway reload watcher causes an infinite restart loop when any plugin is installed via . The gateway starts, runs for ~70 seconds, then kills itself"
---

# Reload watcher triggers infinite restart loop for npm-installed plugins

## 증상
The gateway reload watcher causes an **infinite restart loop** when any plugin is installed via `openclaw plugins install <npm-spec>`. The gateway starts, runs for ~70 seconds, then kills itself with SIGTERM — repeating until launchd gives up and unloads the service entirely.

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Plugins can declare `noopPrefixes` in their reload config to suppress the restart for their own install metadata:

```typescript
reload: {
  configPrefixes: ["channels.botschat"],
  noopPrefixes: ["plugins.installs.botschat"],  // ← workaround
}
```

This works because channel plugin rules are injected before `BASE_RELOAD_RULES_TAIL` in the rule list, and `matchRule()` uses first-match-wins. However, this requires every npm-installed plugin to independently discover and apply the workaround.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41001
