---
layout: solution
title: "🚨 Critical: Default sandbox configuration causes agent startup failure"
category: docker
source: https://github.com/openclaw/openclaw/issues/33877
description: "After a fresh OpenClaw installation or update, all agents fail to start with the following"
---

# 🚨 Critical: Default sandbox configuration causes agent startup failure

## 증상
After a fresh OpenClaw installation or update, **all agents fail to start** with the following error:

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
```bash
openclaw config set agents.defaults.sandbox.mode off
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33877
