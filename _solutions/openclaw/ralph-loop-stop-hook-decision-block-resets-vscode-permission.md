---
layout: solution
title: "Ralph Loop: Stop hook `decision: block` resets VSCode permission mode from bypass to edit"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38511
---

# Ralph Loop: Stop hook `decision: block` resets VSCode permission mode from bypass to edit

## 증상
When using the Ralph Loop plugin in the VSCode extension (Antigravity), each loop iteration resets the permission mode from `bypassPermissions` to `editMode`. This makes Ralph Loop unusable for unattended/overnight autonomous operation.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Created a companion Stop hook that uses Python's sqlite3 module to update the VSCode state database (`state.vscdb`) and force `bypassPermissions` before the session reinitializes:

```bash
#!/bin/bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38511
