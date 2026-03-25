---
layout: solution
title: "LINE channel: monitorLineProvider resolves immediately causing infinite restart loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/24060
---

# LINE channel: monitorLineProvider resolves immediately causing infinite restart loop

## 증상
The LINE channel enters an infinite restart loop immediately after startup. Despite this, messages are received and sent correctly because the webhook endpoint is registered independently of the monitor promise.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
None available through config — `channels.line` has no `healthMonitor` or restart policy options.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/24060
