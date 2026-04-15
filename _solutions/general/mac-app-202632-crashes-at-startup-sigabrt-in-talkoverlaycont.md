---
layout: solution
title: "Mac App 2026.3.2 crashes at startup: SIGABRT in TalkOverlayController.present()"
category: general
source: https://github.com/openclaw/openclaw/issues/35005
description: "Version: Mac App 2026.3.2 (CFBundleVersion"
---

# Mac App 2026.3.2 crashes at startup: SIGABRT in TalkOverlayController.present()

## 증상
**Version:** Mac App 2026.3.2 (CFBundleVersion 2026030290)

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Web dashboard at `127.0.0.1:18789` still works. Gateway runs via LaunchAgent independently.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35005
