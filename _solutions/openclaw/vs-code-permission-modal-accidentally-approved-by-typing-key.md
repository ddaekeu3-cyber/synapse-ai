---
layout: solution
title: "VS Code: Permission modal accidentally approved by typing keystrokes"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37955
description: "When Claude Code is working in the VS Code extension and requests a permission approval, the modal appears while the user is actively typing a prompt. Any"
---

# VS Code: Permission modal accidentally approved by typing keystrokes

## 증상
When Claude Code is working in the VS Code extension and requests a permission approval, the modal appears while the user is actively typing a prompt. Any keystroke being pressed at that moment — **Enter, Space, 1, or 2** — can approve the action in the modal that just appeared. This happens because focus shifts to the modal at the exact moment the user is mid-keystroke.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Using a `PermissionRequest` hook in `.claude/settings.json` to programmatically resolve permission requests before the modal can appear:

```json
"PermissionRequest": [
  {
    "matcher": "Edit|Write|Glob|Grep|Read|Agent",
    "hooks": [{ "type": "command", "command": "echo '{\"decision\": \"allow\"}'", "timeout": 5000 }]
  },
  {
    "matcher": "Bash",
    "hooks": [{ "type": "command", "command": "if echo \"$TOOL_INPUT\" | grep -qiE '(rm -rf|git push --force)'; then echo '{\"decision\": \"deny\"}'; else echo '{\"decision\": \"allow\"}'; fi", "timeout": 5000 }]
  }
]
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37955
