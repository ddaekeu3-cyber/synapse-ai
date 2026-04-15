---
layout: solution
title: "[macOS Node] Missing system.run.prepare command prevents system.run execution"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37591
description: "The macOS/iOS node software (version 2026.3.2) does not implement the command, which causes to fail even when explicitly added to in the node"
---

# [macOS Node] Missing system.run.prepare command prevents system.run execution

## 증상
The macOS/iOS node software (version 2026.3.2) does not implement the `system.run.prepare` command, which causes `system.run` to fail even when explicitly added to `allowCommands` in the node configuration.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Currently using screen recording + ffmpeg frame extraction for screenshots instead of `peekaboo`:
```bash
ffmpeg -i screen_record.mp4 -vframes 1 screenshot.png
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37591
