---
layout: solution
title: "Bug: Write tool permissions do not match paths outside the project directory"
category: config
source: https://github.com/anthropics/claude-code/issues/38391
---

# Bug: Write tool permissions do not match paths outside the project directory

## 증상
The Write tool permission system does not reliably auto-approve writes to paths outside the project directory, regardless of the permission pattern format used. This forces users to manually approve every write to external paths, even when explicit allow rules are configured.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
We use a `PreToolUse` hook on `Write` that checks if the file path contains `claude-task-` and returns `permissionDecision: "allow"`:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Write",
      "hooks": [{
        "type": "command",
        "command": "jq -r 'if .tool_name == \"Write\" and (.tool_input.file_path | test(\"claude-task-\")) then {\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\"}} else {} end'"
      }]
    }]
  }
}
```

This works but is heavyweight for what should be a simple permission rule.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38391
