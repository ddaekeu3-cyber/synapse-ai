---
layout: solution
title: "🐛 Feishu channel fails with tenant_access_token error when HTTP proxy is configured"
category: config
source: https://github.com/openclaw/openclaw/issues/48949
---

# 🐛 Feishu channel fails with tenant_access_token error when HTTP proxy is configured

## 증상
Feishu (飞书) channel cannot send messages when system HTTP proxy is configured. Error: `Cannot destructure property 'tenant_access_token' of '(intermediate value)' as it is undefined.`

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
locally)

**File:** `/opt/homebrew/lib/node_modules/openclaw/node_modules/@larksuiteoapi/node-sdk/lib/index.js`

**Change:**
```javascript
// Before
const defaultHttpInstance = axios__default["default"].create();

// After  
const defaultHttpInstance = axios__default["default"].create({ proxy: false });
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48949
