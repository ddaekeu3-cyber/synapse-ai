---
layout: solution
title: "CLI crashes on Node.js 25 - buffer-equal-constant-time incompatibility"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44918
description: "- openclaw version:"
---

# CLI crashes on Node.js 25 - buffer-equal-constant-time incompatibility

## 증상
- **openclaw version:** 2026.3.12

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually patch the installed package:

```js
// In buffer-equal-constant-time/index.js, change line 37 from:
var origSlowBufEqual = SlowBuffer.prototype.equal;

// To:
var origSlowBufEqual = SlowBuffer && SlowBuffer.prototype ? SlowBuffer.prototype.equal : null;
```

(And similar guards for other `SlowBuffer` references)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44918
