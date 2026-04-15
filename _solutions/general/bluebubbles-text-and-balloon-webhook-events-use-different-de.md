---
layout: solution
title: "BlueBubbles: text and balloon webhook events use different debounce keys, causing duplicate replies"
category: general
source: https://github.com/openclaw/openclaw/issues/31823
description: "When a user sends a message containing a URL, BlueBubbles Server fires two webhook events: one for the text message and one for the URL balloon (link"
---

# BlueBubbles: text and balloon webhook events use different debounce keys, causing duplicate replies

## 증상
When a user sends a message containing a URL, BlueBubbles Server fires two webhook events: one for the text message and one for the URL balloon (link preview). These two events represent the **same logical message**, but the debounce key builder assigns them different keys, so they are processed independently and each triggers a separate agent reply.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Use the same `msg:` prefix for balloons, so `associatedMessageGuid` matches the text's `messageId`:

```diff
  if (balloonBundleId && associatedMessageGuid) {
-   return `bluebubbles:${account.accountId}:balloon:${associatedMessageGuid}`;
+   return `bluebubbles:${account.accountId}:msg:${associatedMessageGuid}`;
  }
```

This way, both events coalesce into the same debounce buffer and `combineDebounceEntries()` merges them into a single `processMessage()` call — which is exactly what it was designed to do.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31823
