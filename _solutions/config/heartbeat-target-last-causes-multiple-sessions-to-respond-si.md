---
layout: solution
title: "heartbeat target: 'last' causes multiple sessions to respond simultaneously"
category: config
source: https://github.com/openclaw/openclaw/issues/43126
description: "When configuring for agents, multiple old sessions respond to heartbeat triggers simultaneously,"
---

# heartbeat target: "last" causes multiple sessions to respond simultaneously

## 증상
When configuring `heartbeat.target: "last"` for agents, multiple old sessions respond to heartbeat triggers simultaneously, causing:

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Manual session cleanup (retaining only 5 most recent per agent):

```bash
for agent_dir in ~/.openclaw/agents/*/sessions; do
  sessions=($(ls -t "$agent_dir"/*.jsonl 2>/dev/null))
  count=0
  for session in "${sessions[@]}"; do
    count=$((count + 1))
    if [ $count -gt 5 ]; then
      mv "$session" ~/.openclaw/agents/$(basename $(dirname "$agent_dir"))/sessions-backup/
    fi
  done
done
```

Result: 180 sessions → 43 sessions (76% reduction)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43126
