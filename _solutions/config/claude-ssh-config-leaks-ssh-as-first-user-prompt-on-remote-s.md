---
layout: solution
title: "claude ssh <config> leaks 'ssh' as first user prompt on remote session"
category: config
source: https://github.com/anthropics/claude-code/issues/38495
description: "When running , the remote Claude Code session receives as the first user message/prompt. This happens because the native binary handles the SSH connection"
---

# claude ssh <config> leaks 'ssh' as first user prompt on remote session

## 증상
When running `claude ssh <config>`, the remote Claude Code session receives `ssh` as the first user message/prompt. This happens because the native binary handles the SSH connection but doesn't strip `ssh` from argv before the JS layer processes it on the remote side.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
A `UserPromptSubmit` hook in `~/.claude/settings.json` can block the leaked prompt:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/script.sh"
          }
        ]
      }
    ]
  }
}
```

Where `script.sh` reads stdin JSON, checks if the prompt field equals `"ssh"`, and exits with code 2 to block it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38495
