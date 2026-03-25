---
layout: solution
title: "[msteams] Inline image downloads fail in 1:1 chats — inbound media uses bot adapter token instead of MSAL Graph token"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28014
---

# [msteams] Inline image downloads fail in 1:1 chats — inbound media uses bot adapter token instead of MSAL Graph token

## 증상
- **OpenClaw:** 2026.2.25 (latest)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
File attachments (drag-and-drop) may use a different download path. Inline paste/screenshots in 1:1 chats are broken.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28014
