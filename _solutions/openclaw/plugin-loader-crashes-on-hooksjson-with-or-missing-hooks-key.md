---
layout: solution
title: "Plugin loader crashes on hooks.json with [] or {} (missing hooks key)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/31763
---

# Plugin loader crashes on hooks.json with [] or {} (missing hooks key)

## 증상
The plugin loader fails to load plugins when `hooks/hooks.json` contains `[]` or `{}` instead of `{"hooks": {}}`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Change `hooks/hooks.json` to `{"hooks": {}}` — but this is fragile since plugin updates can overwrite it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31763
