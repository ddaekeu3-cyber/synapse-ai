---
layout: solution
title: "WhatsApp QR login hangs after scan — 515 restart not handled in startWebLoginWithQr"
category: auth
source: https://github.com/openclaw/openclaw/issues/45756
---

# WhatsApp QR login hangs after scan — 515 restart not handled in startWebLoginWithQr

## 증상
When connecting WhatsApp via the web UI (QR scan), the login process hangs indefinitely after scanning the QR code. The QR is generated and displayed correctly, but after scanning, nothing happens — no connection, no error, just silence.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
A post-build patch on the compiled `login-qr-*.js` files that injects the 515 auto-reconnect handler works as a temporary workaround, but a source-level fix in `startWebLoginWithQr()` would be the proper solution.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45756
