---
layout: solution
title: "`openclaw logs --follow` fails after 2026.3.12 upgrade with handshake timeout while gateway remains healthy"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44714
---

# `openclaw logs --follow` fails after 2026.3.12 upgrade with handshake timeout while gateway remains healthy

## 증상
After upgrading from **2026.3.11** to **2026.3.12**, the gateway remains healthy and reachable locally, but `openclaw logs --follow` (and even one-shot `openclaw logs`) fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Tail the file log directly instead of using `openclaw logs --follow`, e.g.:

```bash
tail -f "$(ls -t /tmp/openclaw/openclaw-*.log | head -n 1)"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44714
