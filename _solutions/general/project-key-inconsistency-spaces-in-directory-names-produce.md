---
layout: solution
title: "Project key inconsistency: spaces in directory names produce two different project keys across versions"
category: general
source: https://github.com/anthropics/claude-code/issues/38007
---

# Project key inconsistency: spaces in directory names produce two different project keys across versions

## 증상
When a working directory path contains spaces (e.g., `C:\Users\user\Drift Analysis`), Claude Code creates two different project keys under `~/.claude/projects/`:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Create a directory junction from the hyphenated key to the space-preserving key:
```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\projects\<hyphenated-key>\memory" -Target "$env:USERPROFILE\.claude\projects\<space-key>\memory" -Force
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38007
