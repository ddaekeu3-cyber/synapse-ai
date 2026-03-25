---
layout: solution
title: "WhatsApp config validation rejects `enabled` key during onboarding"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28443
---

# WhatsApp config validation rejects `enabled` key during onboarding

## 증상
Running `openclaw setup` (or `openclaw channels add`) for WhatsApp fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Add `enabled: z.boolean().optional()` to `WhatsAppConfigSchema`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28443
