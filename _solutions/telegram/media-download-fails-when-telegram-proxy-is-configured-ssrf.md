---
layout: solution
title: "Media download fails when Telegram proxy is configured (SSRF guard overrides proxy dispatcher)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/45467
---

# Media download fails when Telegram proxy is configured (SSRF guard overrides proxy dispatcher)

## 증상
When a Telegram proxy is configured via `channels.telegram.proxy`, media downloads (voice messages, images, files) fail with:

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Adding `pinDns: fetchImpl ? false : undefined` to the `fetchWithSsrFGuard` call in `fetchRemoteMedia()` fixes the issue by skipping DNS pinning when a custom fetch implementation (proxy) is provided:

```js
const result = await fetchWithSsrFGuard(withStrictGuardedFetchMode({
    url,
    fetchImpl,
    init: requestInit,
    maxRedirects,
    policy: ssrfPolicy,
    lookupFn,
    pinDns: fetchImpl ? false : undefined  // skip DNS pinning when proxy is active
}));
```

This preserves SSRF protection for non-proxy scenarios while allowing the proxy dispatcher to handle media downloads correctly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45467
