---
layout: solution
title: "loopDetection: toolCallHistory persists across heartbeat cycles, causing false positives"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/40144
description: "in session state carries over between heartbeat cycles. When a heartbeat runs, its tool calls get pushed onto the history array. Five minutes later, the"
---

# loopDetection: toolCallHistory persists across heartbeat cycles, causing false positives

## 증상
`toolCallHistory` in session state carries over between heartbeat cycles. When a heartbeat runs, its tool calls get pushed onto the history array. Five minutes later, the next heartbeat inherits that history — and if it happens to call similar tools (which heartbeats often do), the loop detector counts them against the accumulated total and fires a false positive.

## 원인
Tool or plugin call failed due to schema mismatch, missing parameter, permission error, or upstream API change. 카테고리: tool-failure.

## 해결법
Patch `recordToolCall()` to clear `toolCallHistory` when the most recent entry is older than 60 seconds:

```js
if (!state.toolCallHistory) state.toolCallHistory = [];
// --- patch start ---
const _lastCall = state.toolCallHistory.at(-1);
if (_lastCall && Date.now() - _lastCall.timestamp > 60000) {
    state.toolCallHistory = [];
}
// --- patch end ---
state.toolCallHistory.push({
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40144
