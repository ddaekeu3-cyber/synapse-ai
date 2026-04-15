---
layout: solution
title: "Feature: On-demand file loading into Memory context mid-session"
category: general
source: https://github.com/anthropics/claude-code/issues/31171
description: "Currently, files can only enter the Memory context (which survives compaction/compression) through two"
---

# Feature: On-demand file loading into Memory context mid-session

## 증상
Currently, files can only enter the **Memory** context (which survives compaction/compression) through two mechanisms:

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
The closest workaround is writing a `CLAUDE.local.md` file mid-session with an `@`-import pointing to the desired file, then triggering a file read to load it as a system reminder. The content loads immediately as a system reminder (in Messages), and only promotes to Memory after compaction re-reads CLAUDE.md from disk. This is fragile, indirect, and not discoverable.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31171
