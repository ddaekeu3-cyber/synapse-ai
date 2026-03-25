---
layout: solution
title: "Discord: ThreadStarterBody re-injected on every turn (echo contamination)"
category: general
source: https://github.com/openclaw/openclaw/issues/41355
---

# Discord: ThreadStarterBody re-injected on every turn (echo contamination)

## 증상
Discord thread starter context (`ThreadStarterBody`) is re-injected into the agent's input on **every turn** of a thread conversation, causing echo contamination. The agent receives `[Thread starter - for context]` repeatedly, which can leak into outbound messages.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Set `includeThreadStarter: false` per-channel in config:
```json
{
  "channels": {
    "discord": {
      "guilds": {
        "<guild-id>": {
          "channels": {
            "*": {
              "includeThreadStarter": false
            }
          }
        }
      }
    }
  }
}
```

This disables thread starter context entirely (including the useful first-turn context).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41355
