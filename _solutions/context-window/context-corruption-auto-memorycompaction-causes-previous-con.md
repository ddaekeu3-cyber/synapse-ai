---
layout: solution
title: "Context corruption: auto-memory/compaction causes previous conversation content to be mixed or truncated"
category: context-window
source: https://github.com/anthropics/claude-code/issues/29175
---

# Context corruption: auto-memory/compaction causes previous conversation content to be mixed or truncated

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
Disabling Auto Memory via settings resolves the compaction failure:

```json
// ~/.claude/settings.json
{
  "autoMemoryEnabled": false
}
```

After disabling, `/compact` works correctly and context is no longer corrupted.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29175
