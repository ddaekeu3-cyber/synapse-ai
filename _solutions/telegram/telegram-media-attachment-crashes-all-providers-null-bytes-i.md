---
layout: solution
title: "Telegram media attachment crashes all providers: null bytes in spawn args"
category: telegram
source: https://github.com/openclaw/openclaw/issues/49973
---

# Telegram media attachment crashes all providers: null bytes in spawn args

## 증상
When a Telegram message includes a media attachment (e.g., a PDF document), the gateway crashes with `ERR_INVALID_ARG_VALUE` because the `[media attached: /path/...]` string passed to `child_process.spawn()` contains null bytes. All three fallback providers fail in sequence, and the user receives no reply.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Sending the message without the attachment works fine. The issue only occurs when a media file is attached.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49973
