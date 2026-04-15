---
layout: solution
title: "Cowork Windows: Triple-parallel VM startup race condition causes 'VM is already running' failure loop"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/32936
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Cowork Windows: Triple-parallel VM startup race condition causes "VM is already running" failure loop

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Race condition or deadlock from multiple concurrent operations targeting the same shared resource without proper locking.

## 해결법
Force-stopping the stale VM before launching Claude Desktop reduces (but doesn't eliminate) the issue:
powershellGet-Process claude -ErrorAction SilentlyContinue | Stop-Process -Force
Stop-VM "cowork-vm" -Force -ErrorAction SilentlyContinue
Start-Sleep 3
Suggested Fix

Add a mutex/lock around VM startup to prevent parallel attempts
Check for and clean up stale VMs before attempting boot
Don't auto-reinstall when the error is "VM is already running" — that means a previous attempt succeeded
Fix the 30-second shutdown timeout so VMs are properly stopped on app quit

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32936
