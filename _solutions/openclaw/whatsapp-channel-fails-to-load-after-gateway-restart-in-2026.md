---
layout: solution
title: "WhatsApp channel fails to load after gateway restart in 2026.3.22"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53767
---

# WhatsApp channel fails to load after gateway restart in 2026.3.22

## 증상
After upgrading to OpenClaw 2026.3.22, the WhatsApp channel stopped loading after gateway restarts. The channel was configured and had valid credentials, but simply did not start.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Updating to 2026.3.23-1 resolved the issue.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53767
