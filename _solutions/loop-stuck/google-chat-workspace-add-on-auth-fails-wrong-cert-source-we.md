---
layout: solution
title: "Google Chat: Workspace Add-on auth fails — wrong cert source, webhook target accumulation from crash loop"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/26332
---

# Google Chat: Workspace Add-on auth fails — wrong cert source, webhook target accumulation from crash loop

## 증상
- OpenClaw version: `2026.2.24`

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
- Use `audienceType: "app-url"` with `audience` set to the exact HTTP endpoint URL
- Patch `monitor.ts` to clear `webhookTargets` before registering (fixes ambiguous target issue)
- Both patches are required for Add-on support to work

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26332
