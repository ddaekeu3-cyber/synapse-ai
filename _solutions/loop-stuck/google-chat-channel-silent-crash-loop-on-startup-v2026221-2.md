---
layout: solution
title: "Google Chat channel silent crash-loop on startup (v2026.2.21-2)"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/23402
---

# Google Chat channel silent crash-loop on startup (v2026.2.21-2)

## 증상
After upgrading from v2026.2.19-2 to v2026.2.21-2, the Google Chat channel immediately crashes on startup and enters a restart loop (up to 10/10 retries). No error message is logged between "starting Google Chat webhook" and "auto-restart attempt". The channel worked on v2026.2.19-2 (with a patch to comment out `abortSignal: ctx.abortSignal` in channel.ts source, though the new version already rem

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
patching out `abortSignal: ctx.abortSignal` from `extensions/googlechat/src/channel.ts` line 555. The compiled JS in v2026.2.21-2 no longer contains that line, but the crash persists with a different (silent) cause. The crash is truly silent — no error, exception, or stack trace is logged between the "starting" and "auto-restart" messages.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23402
