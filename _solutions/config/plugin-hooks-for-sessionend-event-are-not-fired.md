---
layout: solution
title: "Plugin hooks for SessionEnd event are not fired"
category: config
source: https://github.com/anthropics/claude-code/issues/33458
description: "SessionEnd hooks defined in a plugin's are never fired when a session ends. The same hook configuration works correctly when placed in the project's"
---

# Plugin hooks for SessionEnd event are not fired

## 증상
SessionEnd hooks defined in a plugin's `settings.json` are never fired when a session ends. The same hook configuration works correctly when placed in the project's `.claude/settings.local.json`.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Adding the same SessionEnd hook configuration to the **project-level** `.claude/settings.local.json` works correctly. The hook fires, receives `transcript_path` and `reason` as documented.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33458
