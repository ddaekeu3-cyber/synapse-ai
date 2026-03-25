---
layout: solution
title: "memory-lancedb: @lancedb/lancedb missing after npm global install/update"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48244
---

# memory-lancedb: @lancedb/lancedb missing after npm global install/update

## 증상
The `memory-lancedb` extension declares `@lancedb/lancedb` as a dependency in `extensions/memory-lancedb/package.json`, but it is **not listed in the root `package.json`**. When openclaw is installed globally via `npm install -g openclaw`, npm does not process workspace sub-package dependencies, so `@lancedb/lancedb` is never installed.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual install after every update:

```bash
cd /opt/homebrew/lib/node_modules/openclaw && npm install @lancedb/lancedb@"^0.26.2"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48244
