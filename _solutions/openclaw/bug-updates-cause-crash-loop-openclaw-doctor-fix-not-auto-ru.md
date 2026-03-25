---
layout: solution
title: "Bug: Updates cause crash loop - openclaw doctor --fix not auto-run"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/25595
---

# Bug: Updates cause crash loop - openclaw doctor --fix not auto-run

## 증상
OpenClaw updates consistently cause the gateway service to enter a crash loop when plugin validation rules change between versions. The service cannot start because the existing config becomes invalid, but `openclaw doctor --fix` is not automatically executed during updates.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Reinstall OpenClaw entirely (`npm install -g openclaw`), which regenerates a fresh config.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25595
