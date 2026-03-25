---
layout: solution
title: "Browser proxy blocks event loop, kills Telegram polling"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46669
---

# Browser proxy blocks event loop, kills Telegram polling

## 증상
Browser proxy actions (screenshot, click, navigate) that take >20s block the Node.js event loop, preventing Telegram getUpdates polling. After ~115s of stall, Telegram disconnects and never reconnects.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Disable browser proxy on node, use system.run + osascript for Chrome control instead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46669
