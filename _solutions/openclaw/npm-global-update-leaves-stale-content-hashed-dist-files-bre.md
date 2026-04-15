---
layout: solution
title: "npm global update leaves stale content-hashed dist files, breaking imports"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28074
description: "After updating OpenClaw via from a previous version to 2026.2.26, the gateway fails"
---

# npm global update leaves stale content-hashed dist files, breaking imports

## 증상
After updating OpenClaw via `npm install -g openclaw@latest` from a previous version to 2026.2.26, the gateway fails with:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
`npm install -g openclaw@latest --force` resolves it by forcing a full reinstall.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28074
