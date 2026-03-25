---
layout: solution
title: "Control UI: 'paste token in Control UI settings' but no such settings exist"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/26888
---

# Control UI: 'paste token in Control UI settings' but no such settings exist

## 증상
When the Control UI cannot authenticate to the gateway, it shows:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Navigate with token as URL parameter:
```
http://127.0.0.1:18789/?token=<gateway-token>
```

This works — UI picks up the token and connects successfully.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26888
