---
layout: solution
title: "Feature request: hooks.sessionRetention config option"
category: config
source: https://github.com/openclaw/openclaw/issues/30093
---

# Feature request: hooks.sessionRetention config option

## 증상
Add a `sessionRetention` option to the `hooks` configuration block, similar to the existing `cron.sessionRetention` option.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Currently the only workaround is to remove `"sessions"` from `memorySearch.sources` entirely, which also removes useful context from interactive sessions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30093
