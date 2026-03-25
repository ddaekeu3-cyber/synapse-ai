---
layout: solution
title: "[Feature Request] Add configurable Permissions-Policy header for Control UI microphone access"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46921
---

# [Feature Request] Add configurable Permissions-Policy header for Control UI microphone access

## 증상
The Control UI (WebChat) shows a microphone button, but clicking it immediately shows a slash/disabled icon and disappears. This is because the Gateway sets a default HTTP response header:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently, the only workaround is to use a reverse proxy (nginx/Caddy) to override the Permissions-Policy header, which adds complexity.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46921
