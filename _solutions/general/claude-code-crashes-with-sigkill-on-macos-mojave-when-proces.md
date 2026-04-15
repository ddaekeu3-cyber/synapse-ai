---
layout: solution
title: "Claude Code crashes with SIGKILL on macOS Mojave when processing images (sharp/libvips _aligned_alloc)"
category: general
source: https://github.com/anthropics/claude-code/issues/30441
description: "Claude Code is killed by the dynamic linker () on macOS Mojave (10.14) whenever it attempts to process an image — for example when dragging an image into"
---

# Claude Code crashes with SIGKILL on macOS Mojave when processing images (sharp/libvips _aligned_alloc)

## 증상
Claude Code is killed by the dynamic linker (`dyld`) on macOS Mojave (10.14) whenever it attempts to process an image — for example when dragging an image into the terminal or using vision/screenshot features. The process receives `SIGKILL` (signal 9) with no opportunity for graceful error handling.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Downgrade the sharp native binary to version 0.33.0, which pulls in `@img/sharp-libvips-darwin-x64@1.0.0` containing an older libvips built for macOS 10.13+ (no `_aligned_alloc` dependency):

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30441
