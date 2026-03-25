---
layout: solution
title: "Telegram polling infinite loop on document-type media group errors"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/40842
---

# Telegram polling infinite loop on document-type media group errors

## 증상
When document-type messages (e.g., photos sent as documents) arrive in a Telegram media group, the polling loop gets stuck in an infinite retry loop if resolveMedia() throws a non-recoverable error.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Manually increment offset in the update-offset JSON file under agent state directory.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40842
