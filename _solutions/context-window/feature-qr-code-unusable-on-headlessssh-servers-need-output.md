---
layout: solution
title: "[Feature]: QR code unusable on headless/SSH servers — need `--output` flag or HTTP endpoint"
category: context-window
source: https://github.com/openclaw/openclaw/issues/45652
---

# [Feature]: QR code unusable on headless/SSH servers — need `--output` flag or HTTP endpoint

## 증상
`channels login` QR code needs an image output option or HTTP endpoint — Unicode terminal rendering is unscannable on headless/SSH servers.

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
- **Frequency:** Every time WhatsApp needs to be linked or re-linked
- **Consequence:** Hours of debugging and custom scripting to work around a setup step that should take 30 seconds

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45652
