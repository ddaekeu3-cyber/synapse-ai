---
layout: solution
title: "Chrome extension uses stale token for WebSocket relay connection despite correct token saved in options"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50140
---

# Chrome extension uses stale token for WebSocket relay connection despite correct token saved in options

## 증상
Incorrect behavior (not a crash)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Reloading the extension from `chrome://extensions` does not help.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50140
