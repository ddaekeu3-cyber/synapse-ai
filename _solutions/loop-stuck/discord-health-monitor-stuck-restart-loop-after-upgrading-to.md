---
layout: solution
title: "Discord health-monitor stuck-restart loop after upgrading to 2026.3.1"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/31760
description: "After upgrading from 2026.2.26 to 2026.3.1, the Discord provider enters a stuck-restart loop where the health-monitor flags it as \"stuck\" every ~10"
---

# Discord health-monitor stuck-restart loop after upgrading to 2026.3.1

## 증상
After upgrading from 2026.2.26 to 2026.3.1, the Discord provider enters a stuck-restart loop where the health-monitor flags it as "stuck" every ~10 minutes, despite Discord successfully logging in and resolving channels each time.

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
A full gateway restart (`openclaw gateway restart` or SIGUSR1) appears to break the loop temporarily. The health-monitor then stays stable. Rolling back to 2026.2.26 is the safe fallback.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31760
