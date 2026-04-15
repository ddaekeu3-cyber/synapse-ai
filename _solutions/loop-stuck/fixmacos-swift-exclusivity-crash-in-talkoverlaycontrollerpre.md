---
layout: solution
title: "fix(macos): Swift exclusivity crash in TalkOverlayController.present() and VoiceWakeOverlayController.present()"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/33424
description: "Both and crash with (Swift exclusivity violation) on macOS 26.3 (Apple Silicon, M1 MacBook"
---

# fix(macos): Swift exclusivity crash in TalkOverlayController.present() and VoiceWakeOverlayController.present()

## 증상
Both `TalkOverlayController.present()` and `VoiceWakeOverlayController.present()` crash with `SIGABRT` (Swift exclusivity violation) on macOS 26.3 (Apple Silicon, M1 MacBook Air).

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Use a local variable for the `inout` parameter to release exclusive access on `self.model` before the window triggers layout:

```swift
// Before (crashes):
OverlayPanelFactory.present(
    window: self.window,
    isVisible: &self.model.isVisible,
    target: target) { ... }

// After (fixed):
var visible = self.model.isVisible
OverlayPanelFactory.present(
    window: self.window,
    isVisible: &visible,
    target: target) { ... }
self.model.isVisible = visible
```

Applied to both:
- `TalkOverlay.swift` → `TalkOverlayController.present()`
- `VoiceWakeOverlayController+Window.swift` → `Voic

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33424
