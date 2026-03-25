---
layout: solution
title: "Settings file changes should take effect without restarting"
category: config
source: https://github.com/anthropics/claude-code/issues/36826
---

# Settings file changes should take effect without restarting

## 증상
When updating `.claude/settings.local.json` (or other settings files) during a session — for example, adding new permission rules — the changes don't take effect until Claude Code is restarted.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Restart Claude Code after every settings change.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36826
