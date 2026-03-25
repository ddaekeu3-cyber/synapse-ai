---
layout: solution
title: "macOS app crash in MacNodeBrowserProxy.makeRequest (JSON serialization, beta 2026.3.8)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42287
---

# macOS app crash in MacNodeBrowserProxy.makeRequest (JSON serialization, beta 2026.3.8)

## 증상
- **App version:** 2026.3.8-beta.1 (2026030801)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Disabling **Browser Control** in the macOS app menu prevents the crash, since the gateway won't send browser proxy invokes to the node.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42287
