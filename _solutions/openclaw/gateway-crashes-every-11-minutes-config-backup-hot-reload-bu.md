---
layout: solution
title: "Gateway Crashes Every 11 Minutes - Config Backup + Hot Reload Bug"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49188
---

# Gateway Crashes Every 11 Minutes - Config Backup + Hot Reload Bug

## 증상
Gateway crashes every 11 minutes due to config backup triggering hot reload with lock file cleanup failure.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Auto-restart script (not sustainable):
```powershell

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49188
