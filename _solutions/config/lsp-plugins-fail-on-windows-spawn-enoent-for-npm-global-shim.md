---
layout: solution
title: "LSP plugins fail on Windows: spawn ENOENT for npm global shim scripts"
category: config
source: https://github.com/anthropics/claude-code/issues/37897
---

# LSP plugins fail on Windows: spawn ENOENT for npm global shim scripts

## 증상
All LSP plugins (`pyright-lsp`, `typescript-lsp`, etc.) fail on Windows because Claude Code uses `child_process.spawn()` without `shell: true` to launch LSP server binaries. On Windows, npm installs global packages as shell shim scripts (`.cmd`/`.ps1`/bash), not native executables, so `spawn()` fails with ENOENT.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Add `shell: true` to the `spawn()` options when launching LSP servers on Windows, or detect and use the `.cmd` shim directly. This affects all LSP plugins, not just pyright.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37897
