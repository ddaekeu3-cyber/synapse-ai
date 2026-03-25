---
layout: solution
title: "macOS app: Canvas overlay covers permission dialog, creating deadlock"
category: concurrency
source: https://github.com/openclaw/openclaw/issues/28473
---

# macOS app: Canvas overlay covers permission dialog, creating deadlock

## 증상
When `canvas.present` is invoked by the agent, the Canvas overlay renders at a higher z-order (window level) than the "Allow this command?" permission dialog in the macOS app. This creates a deadlock: the user cannot see or interact with the permission dialog to approve or deny the command, and the Canvas overlay won't dismiss until the command completes.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
Send a blind Return keypress via AppleScript to approve the hidden dialog:

```bash
osascript -e 'tell application "System Events" to key code 36'
```

This works because the permission dialog still has keyboard focus even though it's visually obscured. However, this is fragile — it blindly approves whatever dialog is pending without the user being able to read it, which defeats the purpose of the permission system.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28473
