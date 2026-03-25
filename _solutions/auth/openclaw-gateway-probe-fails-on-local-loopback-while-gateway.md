---
layout: solution
title: "openclaw gateway probe fails on local loopback while gateway health / status / cron add succeed Summary"
category: auth
source: https://github.com/openclaw/openclaw/issues/53443
---

# openclaw gateway probe fails on local loopback while gateway health / status / cron add succeed Summary

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #53443에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Make gateway probe reuse the same connection/auth path as normal CLI RPC calls where possible, so that:
probe
health
status
cron
do not disagree so sharply on whether the same loopback gateway is reachable.
Additional improvement
Consider not disabling deviceIdentity on loopback probe, or at least ensure that the probe mode is not materially weaker/more fragile than normal CLI RPC mode.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53443
