---
layout: solution
title: "--dangerously-skip-permissions flag should bypass the permissions mode dialog without requiring persisted setting"
category: config
source: https://github.com/anthropics/claude-code/issues/25503
---

# --dangerously-skip-permissions flag should bypass the permissions mode dialog without requiring persisted setting

## 증상
When launching Claude Code with `--dangerously-skip-permissions`, the "WARNING: Claude Code running in Bypass Permissions mode" dialog is shown on every launch unless `skipDangerousModePermissionPrompt: true` is persisted in `settings.json`.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Manually add `"skipDangerousModePermissionPrompt": true` to `settings.json` and ensure it's committed if the config directory is git-managed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25503
