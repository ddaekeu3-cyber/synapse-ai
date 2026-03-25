---
layout: solution
title: "macOS app crashes with fatal access conflict in TalkOverlayController.present() when voice wake is enabled"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34903
---

# macOS app crashes with fatal access conflict in TalkOverlayController.present() when voice wake is enabled

## 증상
The macOS desktop app crashes on launch when Voice Wake / Talk Mode is enabled. The crash occurs in `TalkOverlayController.present()` with a `Fatal access conflict detected` error.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Disable voice wake/talk mode in config before launching. The CLI gateway continues to work fine.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34903
