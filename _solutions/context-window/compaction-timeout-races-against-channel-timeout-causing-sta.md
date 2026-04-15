---
layout: solution
title: "Compaction timeout races against channel timeout, causing stale-response loop"
category: context-window
source: https://github.com/openclaw/openclaw/issues/25272
description: "When compaction triggers on a Telegram channel, three competing timeout layers race against each other. If the channel timeout fires first, it delivers a"
---

# Compaction timeout races against channel timeout, causing stale-response loop

## 증상
When compaction triggers on a Telegram channel, three competing timeout layers race against each other. If the channel timeout fires first, it delivers a stale cached response and aborts the in-flight compaction. Since context is still over threshold, compaction immediately retriggers — creating a deterministic loop of stale responses until manual intervention.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Set the channel timeout above the compaction timeout:

```json
{
  "channels": {
    "telegram": {
      "timeoutSeconds": 600
    }
  }
}
```

This stops the loop immediately — the channel waits long enough for compaction to finish. Additionally, lowering `contextTokens` (e.g. 128000) triggers compaction earlier on smaller context, which Opus can summarize within the 5-min window.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25272
