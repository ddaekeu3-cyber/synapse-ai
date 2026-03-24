---
layout: solution
title: "Compaction timeout races against channel timeout, causing stale-response loop"
category: telegram
---

# Compaction timeout races against channel timeout, causing stale-response loop

## 증상
When compaction triggers on a Telegram channel, three competing timeout layers race against each other. If the channel timeout fires first, it delivers a stale cached response and aborts the in-flight compaction. Since context is still over threshold, compaction immediately retriggers — creating a d

에러 메시지:
```
Run 1 (23:03:17 → 23:07:17):
  compaction start → compaction wait aborted (timeout)
  "using current snapshot: timed out during compaction"
  "compaction promise rejected: AbortError: Unsubscribed

## 원인
원본 이슈에서 확인 필요. GitHub Issue #25272 참조.

## 해결법
Set the channel timeout above the compaction timeout:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/25272
