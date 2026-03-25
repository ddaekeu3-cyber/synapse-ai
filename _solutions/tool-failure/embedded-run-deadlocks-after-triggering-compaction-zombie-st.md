---
layout: solution
title: "Embedded run deadlocks after triggering compaction (zombie state until timeout)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/48518
---

# Embedded run deadlocks after triggering compaction (zombie state until timeout)

## 증상
Embedded runs that trigger context compaction mid-run get stuck in a zombie state. The run completes its actual work, compaction fires, but the compaction retry never resolves. The session stays in `active=true` until the 600s timeout kills it.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
Restart the service (`systemctl restart clawdbot.service`). The run is lost but the session recovers.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48518
