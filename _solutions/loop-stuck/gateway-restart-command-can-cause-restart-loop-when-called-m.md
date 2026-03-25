---
layout: solution
title: "gateway restart command can cause restart loop when called multiple times"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/33459
---

# gateway restart command can cause restart loop when called multiple times

## 증상
When the `openclaw gateway restart` command is called multiple times in quick succession (e.g., user requests restart multiple times, or multiple restart commands are issued), it can cause a restart loop where the gateway repeatedly stops and starts.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Created a wrapper script (`~/.openclaw/safe-restart.sh`) with a file-based lock to prevent this, but this should be built into the CLI itself.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33459
