---
layout: solution
title: "Compaction summaries absorb system prompt content, causing unbounded growth and stale context"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/48547
---

# Compaction summaries absorb system prompt content, causing unbounded growth and stale context

## 증상
Compaction summaries monotonically grow because the summarization model includes system prompt content (from contextFiles like SOUL.md, AGENTS.md, MEMORY.md, TOOLS.md, etc.) in the summary output. Over many compaction cycles, this causes:

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
Setting `agents.defaults.compaction.customInstructions` to explicitly exclude system prompt content:

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "customInstructions": "Do NOT include system prompt content in the summary. Exclude: authorized sender lists, workspace-critical-rules, available_skills, SOUL.md/AGENTS.md/TOOLS.md content, HEARTBEAT protocols, and runtime metadata. Only summarize actual conversation content."
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48547
