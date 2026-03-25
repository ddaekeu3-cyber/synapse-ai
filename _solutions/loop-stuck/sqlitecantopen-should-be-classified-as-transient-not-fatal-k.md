---
layout: solution
title: "SQLITE_CANTOPEN should be classified as transient, not fatal (kills LaunchAgent)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/34678
---

# SQLITE_CANTOPEN should be classified as transient, not fatal (kills LaunchAgent)

## 증상
`SQLITE_CANTOPEN` errors cause the gateway process to exit when running as a LaunchAgent on macOS. The same error is survived in terminal mode because it's caught by local try/catch handlers, but when it escapes to the global `unhandledRejection` handler, it falls through to `process.exit(1)` because it's not in any safe-list.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Manually patching the compiled dist files to add SQLite codes to `TRANSIENT_NETWORK_CODES`. This survives until the next `openclaw update`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34678
