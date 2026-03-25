---
layout: solution
title: "Node host WebSocket retry gets stuck after Cloudflare 502 errors"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/50219
---

# Node host WebSocket retry gets stuck after Cloudflare 502 errors

## 증상
- Node host: macOS, launchd agent (KeepAlive: true)

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Manual `openclaw node restart` on the Mac restores connectivity immediately.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50219
