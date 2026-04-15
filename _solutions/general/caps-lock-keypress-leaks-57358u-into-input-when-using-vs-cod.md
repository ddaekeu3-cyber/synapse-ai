---
layout: solution
title: "Caps Lock keypress leaks '[57358u' into input when using VS Code integrated terminal"
category: general
source: https://github.com/anthropics/claude-code/issues/38581
description: "- Claude Code in VS Code integrated"
---

# Caps Lock keypress leaks '[57358u' into input when using VS Code integrated terminal

## 증상
- Claude Code in VS Code integrated terminal

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Disabling `terminal.integrated.enableKittyProtocol` in VS Code fixes the noise, but breaks Shift+Enter (newline in Claude Code input) since the terminal can no longer distinguish Shift+Enter from Enter without the Kitty protocol.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38581
