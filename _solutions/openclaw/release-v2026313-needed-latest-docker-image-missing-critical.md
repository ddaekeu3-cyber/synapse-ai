---
layout: solution
title: "Release v2026.3.13 needed: :latest Docker image missing critical bug fixes"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45324
---

# Release v2026.3.13 needed: :latest Docker image missing critical bug fixes

## 증상
The `:latest` Docker tag on GHCR still points to `v2026.3.12`, which does **not** include the 5 High-severity bug fixes merged to `main` after the release.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Users can pull `:main` tag in the meantime:
```
docker pull ghcr.io/openclaw/openclaw:main
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45324
