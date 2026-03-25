---
layout: solution
title: "ralph-loop plugin stop-hook causes infinite loop when Windows username contains spaces"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/33633
---

# ralph-loop plugin stop-hook causes infinite loop when Windows username contains spaces

## 증상
The `ralph-loop` plugin's `stop-hook.sh` causes an infinite error loop when the Windows username contains spaces (e.g., `Guiquan Wang`). Every response from Claude triggers the hook, which fails, and the error feedback triggers another response, creating an unbreakable infinite loop.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. Disable `ralph-loop` in `settings.json` by setting `"ralph-loop@claude-plugins-official": false` (or removing it)
2. Replace all `stop-hook.sh` files with `#!/bin/bash\nexit 0`
3. Clear `hooks.json` registration: `{"description":"disabled","hooks":{}}`
4. **Restart** the Claude Code session (changes don't take effect in the current session)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33633
