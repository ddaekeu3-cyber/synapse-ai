---
layout: solution
title: "Qwen OAuth refresh token expired or invalid — requires frequent re-authentication since v2026.3.2"
category: auth
source: https://github.com/openclaw/openclaw/issues/36982
---

# Qwen OAuth refresh token expired or invalid — requires frequent re-authentication since v2026.3.2

## 증상
- **Version:** 2026.3.2 (and previous version)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Manual re-authentication:
```
openclaw models auth login --provider qwen-portal
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36982
