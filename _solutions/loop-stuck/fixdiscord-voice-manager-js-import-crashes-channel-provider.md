---
layout: solution
title: "fix(discord): voice manager .js import crashes channel provider on startup"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/50047
---

# fix(discord): voice manager .js import crashes channel provider on startup

## 증상
The Discord channel provider crashes in a restart loop on every startup with:

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Change `.js` → `.ts` in the import specifiers of `*.runtime.ts` files that are loaded via jiti at runtime (not compiled to JS). These files are always executed as TypeScript source through the plugin loader.

**Note:** Many other extensions have the same `.js` import pattern in their `*.runtime.ts` files. They may not be crashing yet (perhaps their imports resolve to bundled dist chunks), but they are at risk.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50047
