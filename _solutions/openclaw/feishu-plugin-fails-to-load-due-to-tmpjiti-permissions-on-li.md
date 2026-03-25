---
layout: solution
title: "Feishu plugin fails to load due to /tmp/jiti permissions on Linux"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31785
---

# Feishu plugin fails to load due to /tmp/jiti permissions on Linux

## 증상
The Feishu plugin fails to load on Linux systems due to permission issues with the `/tmp/jiti` directory, which is used for TypeScript compilation by the `jiti` module.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```bash
sudo chmod 777 /tmp/jiti
openclaw gateway restart
```

This fix is temporary and requires manual intervention after each reboot.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31785
