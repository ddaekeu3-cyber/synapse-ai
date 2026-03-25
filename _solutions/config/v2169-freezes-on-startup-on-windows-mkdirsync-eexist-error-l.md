---
layout: solution
title: "v2.1.69: Freezes on startup on Windows - mkdirSync EEXIST error loop"
category: config
source: https://github.com/anthropics/claude-code/issues/30960
---

# v2.1.69: Freezes on startup on Windows - mkdirSync EEXIST error loop

## 증상
Claude Code v2.1.69 freezes on startup on Windows. Downgrading to v2.1.66 resolves the issue.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Downgrade to v2.1.66:
```powershell

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30960
