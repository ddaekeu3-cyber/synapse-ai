---
layout: solution
title: "macOS app crash-loops when Talk mode enabled (TalkOverlayView Swift exclusivity error)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37114
description: "- OpenClaw version: 2026.3.2"
---

# macOS app crash-loops when Talk mode enabled (TalkOverlayView Swift exclusivity error)

## 증상
- **OpenClaw version**: 2026.3.2 (85377a2)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Reset the pref to recover:
```bash
defaults write ai.openclaw.mac "openclaw.talkEnabled" -bool false
open -a OpenClaw
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37114
