---
layout: solution
title: "Telegram media download intermittently fails with TypeError: fetch failed in Docker"
category: telegram
---

# Telegram media download intermittently fails with TypeError: fetch failed in Docker

## 증상
Telegram DM photo uploads intermittently fail with `MediaFetchError` at the file download step (`https://api.telegram.org/file/bot...`), even though Telegram updates and text messages are received normally.

에러 메시지:
`MediaFetchError`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49008 참조.

## 해결법
1. Add built-in retry to Telegram file download path (not just `getFile`).
2. Improve diagnostics for `fetchRemoteMedia` failures:
   - include `cause`
   - classify socket/DNS/TLS/timeout where possible
3. Consider fallback transport (`https.get` or explicit undici strategy) for Telegram file downloads.
4. Validate whether intermittent `fetch failed` reproduces in Node 22+/24 with Docker bridge n

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49008
