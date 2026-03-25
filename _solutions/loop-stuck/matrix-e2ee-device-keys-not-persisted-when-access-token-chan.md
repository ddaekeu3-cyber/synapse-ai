---
layout: solution
title: "Matrix E2EE device keys not persisted when access token changes"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/48749
---

# Matrix E2EE device keys not persisted when access token changes

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #48749에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Remove token hash from storage path:
```typescript
const rootDir = path.join(
  stateDir,
  "matrix",
  "accounts",
  accountKey,
  `${serverKey}__${userKey}`,
  "e2ee",  // Fixed directory
);
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48749
