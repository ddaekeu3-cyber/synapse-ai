---
layout: solution
title: "Bug: `/plugin enable` fails for user-scope installed plugins"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38084
description: "The command fails to recognize plugins installed with , reporting \"not installed in this project\" even though the plugin is correctly installed in the"
---

# Bug: `/plugin enable` fails for user-scope installed plugins

## 증상
The `/plugin enable` command fails to recognize plugins installed with `--scope user`, reporting "not installed in this project" even though the plugin is correctly installed in the user scope and exists in the cache.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually edit `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "skill-creator@claude-plugins-official": true
  }
}
```

Then run `/reload-plugins` or restart Claude Code.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38084
