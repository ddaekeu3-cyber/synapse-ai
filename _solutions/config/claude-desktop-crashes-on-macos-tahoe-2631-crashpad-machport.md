---
layout: solution
title: "Claude Desktop crashes on macOS Tahoe 26.3.1 — Crashpad mach_port_request_notification failure"
category: config
source: https://github.com/anthropics/claude-code/issues/37230
---

# Claude Desktop crashes on macOS Tahoe 26.3.1 — Crashpad mach_port_request_notification failure

## 증상
Claude Desktop (v1.1.7714) crashes repeatedly on macOS Tahoe 26.3.1. Started the week of March 12, 2026. Same hardware/OS configuration works fine on a second machine.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
## What Was Tried (all failed to resolve)
1. Full uninstall/reinstall via `brew reinstall --cask claude` (multiple times)
2. Complete deletion of `~/Library/Application Support/Claude/`
3. Deleting Cookies, Preferences, Keychain entries (`Claude Safe Storage`)
4. `tccutil reset All com.anthropic.claudefordesktop`
5. Removing quarantine: `xattr -cr /Applications/Claude.app`
6. Launch flags: `--disable-gpu`, `--disable-crash-reporter`, `--password-store=basic`, `--disable-breakpad`
7. Environment variables: `ELECTRON_DISABLE_CRASH_REPORTER=1`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37230
