---
layout: solution
title: "ACP parent session stuck until refresh when yielded waiting for child completion"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52249
description: "When an ACP child session completes while a parent session is yielded waiting for the result, the parent session remains stuck/non-responsive until the"
---

# ACP parent session stuck until refresh when yielded waiting for child completion

## 증상
When an ACP child session completes while a parent session is yielded waiting for the result, the parent session remains stuck/non-responsive until the user manually refreshes the UI.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Applied

We deployed a Phase A stabilization patch that changed the relay to:
```
always: enqueueSystemEvent() + requestHeartbeatNow()
never: direct resumeYieldedParent() call
```

This forces all ACP completion follow-ups back onto the existing system-event + heartbeat wake path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52249
