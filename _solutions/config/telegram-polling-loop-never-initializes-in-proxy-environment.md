---
layout: solution
title: "Telegram polling loop never initializes in proxy environments (China/HTTP_PROXY required)"
category: config
source: https://github.com/openclaw/openclaw/issues/46888
---

# Telegram polling loop never initializes in proxy environments (China/HTTP_PROXY required)

## 증상
Telegram provider fails to initialize polling loop when HTTP_PROXY is required for Telegram API access (e.g., China region). The `bot.start()` method in grammY appears to not inherit the `HTTP_PROXY` environment variable correctly, causing the long-poll connection to fail silently while outbound `sendMessage` calls work fine.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
should:

1. **Pass explicit proxy agent to grammY**: Configure grammY's `bot.start()` with an explicit agent that uses the configured HTTP_PROXY URL
2. **Proxy-aware initialization**: Ensure the polling fetch uses the same proxy settings as outbound calls
3. **Error logging**: Log when polling fails to establish through proxy (currently silent)

Example approach:
```typescript
import { HttpsProxyAgent } from 'https-proxy-agent';

const proxyAgent = new HttpsProxyAgent(HTTP_PROXY);
const bot = new Bot(token, {
  client: {
    agent: proxyAgent
  }
});
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46888
