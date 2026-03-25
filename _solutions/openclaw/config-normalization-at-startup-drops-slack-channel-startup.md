---
layout: solution
title: "Config normalization at startup drops Slack channel startup task (cdd797f)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53143
---

# Config normalization at startup drops Slack channel startup task (cdd797f)

## 증상
After upgrading from `d41c9ad` to `cdd797f` (2026.3.23-beta.1), the gateway's Slack Socket Mode channel never starts. The gateway completes plugin loading and reaches `listening on ws://0.0.0.0:18789`, but the Slack channel `startAccount` / `app.start()` phase is never entered.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Staying on `d41c9ad` until this is resolved.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53143
