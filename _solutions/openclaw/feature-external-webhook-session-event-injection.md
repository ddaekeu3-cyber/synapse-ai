---
layout: solution
title: "Feature: External webhook → session event injection"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46806
---

# Feature: External webhook → session event injection

## 증상
External services (n8n, monitoring tools, etc.) cannot trigger events in an OpenClaw session. For example:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently using HEARTBEAT.md to poll n8n for errors, but this has 30+ minute delay.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46806
