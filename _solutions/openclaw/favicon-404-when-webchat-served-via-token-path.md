---
layout: solution
title: "Favicon 404 when webchat served via /token/ path"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48378
---

# Favicon 404 when webchat served via /token/ path

## 증상
When accessing the webchat/control UI via the `/token/` path (e.g. `https://127.0.0.1:18789/token/`), the favicon fails to load with a 404.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None without modifying upstream files (which get overwritten on update).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48378
