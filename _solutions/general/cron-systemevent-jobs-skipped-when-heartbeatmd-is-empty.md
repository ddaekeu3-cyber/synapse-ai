---
layout: solution
title: "Cron systemEvent jobs skipped when HEARTBEAT.md is empty"
category: general
source: https://github.com/openclaw/openclaw/issues/33090
---

# Cron systemEvent jobs skipped when HEARTBEAT.md is empty

## 증상
Cron jobs with `payload.kind: 'systemEvent'` are being skipped with error `empty-heartbeat-file` when `HEARTBEAT.md` is empty or contains only comments.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Adding any non-comment content to HEARTBEAT.md (e.g., `active: true`) allows cron jobs to run.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33090
