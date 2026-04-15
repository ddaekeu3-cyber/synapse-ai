---
layout: solution
title: "Gateway JWT middleware blocks Bot Framework webhooks in msteams plugin"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/14436
description: "The OpenClaw gateway applies JWT authentication middleware globally to all routes, which blocks legitimate Microsoft Bot Framework webhooks at . This"
---

# Gateway JWT middleware blocks Bot Framework webhooks in msteams plugin

## 증상
The OpenClaw gateway applies JWT authentication middleware globally to all routes, which blocks legitimate Microsoft Bot Framework webhooks at `/api/messages`. This prevents the msteams plugin from receiving webhooks from Azure Bot Service.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Created a reverse proxy that strips OpenClaw JWT requirements:

```
// Proxy: localhost:4000 -> localhost:3978
app.all('/api/messages', async (req, res) => {
  const headers = { ...req.headers };
  delete headers['x-openclaw-token'];
  
  const response = await axios({
    method: req.method,
    url: 'http://localhost:3978/api/messages',
    data: req.body,
    headers
  });
  
  res.status(response.status).send(response.data);
});
```

**Architecture with workaround:**

```
Bot Service → Cloudflare Tunnel → Proxy (4000) → OpenClaw msteams (3978)
                                   ↑ JWT bypas

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/14436
