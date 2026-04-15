---
layout: solution
title: "Desktop Cowork: RemotePluginManager removes 3rd-party GitHub marketplace plugins on every sync"
category: config
source: https://github.com/anthropics/claude-code/issues/38429
description: "Claude Desktop (Cowork mode) removes all plugins from third-party GitHub-sourced marketplaces after every restart. The cleanup marks them as because it"
---

# Desktop Cowork: RemotePluginManager removes 3rd-party GitHub marketplace plugins on every sync

## 증상
Claude Desktop (Cowork mode) removes all plugins from third-party GitHub-sourced marketplaces after every restart. The `RemotePluginManager.syncPlugins()` cleanup marks them as `NOT_AVAILABLE` because it only protects `source: "manual"` marketplaces, not `source: "github"` ones.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Use Claude Code CLI (`claude plugin install <name>@<marketplace>`) instead of the Desktop Cowork GUI. CLI-installed plugins use the global `~/.claude/plugins/installed_plugins.json` and are not subject to `RemotePluginManager` sync.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38429
