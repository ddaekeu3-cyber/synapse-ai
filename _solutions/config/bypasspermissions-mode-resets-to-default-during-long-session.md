---
layout: solution
title: "bypassPermissions mode resets to 'default' during long sessions at system boundaries"
category: config
source: https://github.com/anthropics/claude-code/issues/38372
description: "The set to in settings (global, user-local, and project-local) spontaneously resets to during long-running sessions, causing unexpected permission prompts"
---

# bypassPermissions mode resets to 'default' during long sessions at system boundaries

## 증상
The `permissionMode` set to `bypassPermissions` in settings (global, user-local, and project-local) spontaneously resets to `default` during long-running sessions, causing unexpected permission prompts for Edit/Write operations.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
A PreToolUse hook that forces `permissionDecision: "allow"` for Edit/Write/MultiEdit tools:

```json
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/auto-approve-edits.sh"
          }
        ]
      }
    ]
  }
}
```

```bash
#!/bin/bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38372
