---
layout: solution
title: "Heartbeat `every` interval >24.8 days triggers infinite TimeoutOverflowWarning loop (CPU/memory storm)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/28405
---

# Heartbeat `every` interval >24.8 days triggers infinite TimeoutOverflowWarning loop (CPU/memory storm)

## 증상
When `agents.defaults.heartbeat.every` is set to a duration exceeding Node.js's `setTimeout` 32-bit signed integer limit (~24.85 days / 2,147,483,647ms), the gateway enters an infinite loop of `TimeoutOverflowWarning`, consuming 100% CPU, leaking memory at ~500MB/min, and producing ~17MB/s of stderr output.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Change `heartbeat.every` to a value under 24.8 days (e.g., `"8h"`):
```json
{
  "agents": {
    "defaults": {
      "heartbeat": {
        "every": "8h"
      }
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28405
