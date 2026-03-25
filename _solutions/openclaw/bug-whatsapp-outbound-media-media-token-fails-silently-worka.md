---
layout: solution
title: "Bug: WhatsApp outbound media (MEDIA: token) fails silently — workaround: openclaw message send --media"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54131
---

# Bug: WhatsApp outbound media (MEDIA: token) fails silently — workaround: openclaw message send --media

## 증상
When an agent includes a `MEDIA:` path in its reply text, the file is **never delivered** to WhatsApp. The outbound log always shows `mediaUrl=null`, `mediaKind=null`. No error is logged.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
was found via community PDF
- Running as LaunchDaemon (not LaunchAgent) — unsure if this affects file system access

Thanks for the amazing tool 🦞

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54131
