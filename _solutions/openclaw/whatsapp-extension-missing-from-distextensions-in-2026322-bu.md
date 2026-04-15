---
layout: solution
title: "WhatsApp extension missing from dist/extensions/ in 2026.3.22 build"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53424
description: "After upgrading from to , the WhatsApp channel is completely unavailable. The gateway"
---

# WhatsApp extension missing from dist/extensions/ in 2026.3.22 build

## 증상
After upgrading from `2026.3.11` to `2026.3.22`, the WhatsApp channel is completely unavailable. The gateway logs:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Downgrade to 2026.3.11:

```bash
npm i -g openclaw@2026.3.11
systemctl restart openclaw
```

WhatsApp connects immediately on 3.11.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53424
