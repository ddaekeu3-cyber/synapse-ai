---
layout: solution
title: "Windows: exec tool creates visible console windows (conhost flash)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/22851
---

# Windows: exec tool creates visible console windows (conhost flash)

## 증상
On Windows, every exec tool call spawns a visible console window (conhost.exe) that briefly flashes on screen. When cron jobs run frequently (e.g. every 10 minutes), this creates a distracting flash on the user's desktop.

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
is to pass windowsHide: true in the spawn options.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22851
