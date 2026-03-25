---
layout: solution
title: "Cron isolated sessions timing out after 2026.3.8 update"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40868
---

# Cron isolated sessions timing out after 2026.3.8 update

## 증상
After updating to OpenClaw 2026.3.8, cron jobs with `sessionTarget: "isolated"` consistently time out. The same tasks work fine when run manually in the main session.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Changing `sessionTarget` from "isolated" to "main" should work, but this is not ideal for isolated cron jobs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40868
