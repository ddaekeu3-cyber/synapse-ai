---
layout: solution
title: "feat: support prompt/agent hook types for PreCompact/PostCompact events"
category: general
source: https://github.com/anthropics/claude-code/issues/36749
---

# feat: support prompt/agent hook types for PreCompact/PostCompact events

## 증상
When using the auto-memory system, important information discovered during long conversations can be lost during context compaction if not saved to memory files in time. The `PreCompact` and `PostCompact` hooks exist, but they only support `command` type hooks — not `prompt` or `agent` types.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Using a `command` hook on `PostCompact` that returns `additionalContext` to remind Claude to check for unsaved memories:

```bash
#!/bin/bash
cat <<'INNER_EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostCompact",
    "additionalContext": "Context compaction just occurred. Review the summary and save any important unsaved information to memory files."
  }
}
INNER_EOF
```

This works but is less reliable than a direct agent action.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36749
