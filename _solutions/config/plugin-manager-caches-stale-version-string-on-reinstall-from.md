---
layout: solution
title: "Plugin manager caches stale version string on reinstall from private marketplace"
category: config
source: https://github.com/anthropics/claude-code/issues/38478
description: "When reinstalling a plugin from a private Git-based marketplace, the plugin manager writes the previous version string to and the cache directory name,"
---

# Plugin manager caches stale version string on reinstall from private marketplace

## 증상
When reinstalling a plugin from a private Git-based marketplace, the plugin manager writes the **previous** version string to `installed_plugins.json` and the cache directory name, even though the marketplace source files all contain the correct (newer) version.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Manually edit `installed_plugins.json` to fix the version string and rename the cache directory:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38478
