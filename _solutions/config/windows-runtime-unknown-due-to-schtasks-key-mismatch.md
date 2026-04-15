---
layout: solution
title: "Windows: Runtime: unknown due to schtasks key mismatch"
category: config
source: https://github.com/openclaw/openclaw/issues/47726
description: "Regression (worked before, now"
---

# Windows: Runtime: unknown due to schtasks key mismatch

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #47726에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Update `parseSchtasksQuery` to also check for `last result` (without "run") as a fallback:

```javascript
const lastRunResult = entries["last run result"] ?? entries["last result"];
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47726
