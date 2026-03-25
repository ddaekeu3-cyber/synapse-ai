---
layout: solution
title: "macOS voice mode: cannot re-activate after 120s timeout without switching windows"
category: performance
source: https://github.com/openclaw/openclaw/issues/49448
---

# macOS voice mode: cannot re-activate after 120s timeout without switching windows

## 증상
**OS:** macOS 26 (Darwin 25.3.0, arm64)

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Switch to another window and back to restore voice mode.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49448
