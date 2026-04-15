---
layout: solution
title: "Claude Desktop fails to launch on Windows 11 Home — 'Claude Desktop failed to Launch' error dialog on every attempt"
category: config
source: https://github.com/anthropics/claude-code/issues/25194
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Claude Desktop fails to launch on Windows 11 Home — "Claude Desktop failed to Launch" error dialog on every attempt

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
the issue
   → Installer detects: "WARNING: CoworkVMService already exists (potential conflict)"
   → Installer tries to remove it but fails:
     "WARNING: failed to remove conflicting service:
      could not open CoworkVMService: Access is denied."
   → Reinstall completes with INSTALL_SUCCESS but launch still fails
7. Repeat step 6 multiple times (confirmed in log: 10+ reinstall attempts)
   → Same result every time — CoworkVMService cannot be removed
   → Claude Desktop never launches successfully

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25194
