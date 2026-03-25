---
layout: solution
title: "Heartbeat alternates between sent and ok-token every cycle — effective interval is 2x configured"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47940
---

# Heartbeat alternates between sent and ok-token every cycle — effective interval is 2x configured

## 증상
- OpenClaw 2026.3.13, macOS, GPT-5.4

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using a cron-based heartbeat instead of the native `heartbeat.every` scheduler, as suggested in Discussion #11042, would give each tick an independent context.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47940
